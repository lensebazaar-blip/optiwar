"""Tests for ACR Step 5 — closure / sweeper primitives.

Pure, DB/Flask-free coverage of the deterministic outcome decision and of the
two sweeps' dry-run vs live behaviour, edge-triggering and idempotency, using a
tiny in-memory fake that mimics just enough of the DB-API cursor contract
(execute / fetchone / fetchall / rowcount) exercised by acr.py.

    python3 -m unittest tests.test_acr_closure
"""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_acr():
    spec = importlib.util.spec_from_file_location(
        "acr_under_test_closure", os.path.join(REPO, "acr.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


acr = _load_acr()


class DecideOutcomeTests(unittest.TestCase):
    def test_priority_order(self):
        # PURCHASED beats everything.
        self.assertEqual(acr.decide_session_outcome(
            has_order=True, is_escalated=True, is_failed=True,
            normally_resolved=True), acr.OUTCOME_PURCHASED)
        # ESCALATED beats FAILED/ANSWERED.
        self.assertEqual(acr.decide_session_outcome(
            is_escalated=True, is_failed=True, normally_resolved=True),
            acr.OUTCOME_ESCALATED)
        # FAILED beats ANSWERED/ABANDONED.
        self.assertEqual(acr.decide_session_outcome(
            is_failed=True, normally_resolved=True), acr.OUTCOME_FAILED)
        # Normal close with no higher truth -> ANSWERED.
        self.assertEqual(acr.decide_session_outcome(normally_resolved=True),
                         acr.OUTCOME_ANSWERED)
        # Archived, nothing else -> ABANDONED (matured candidate).
        self.assertEqual(acr.decide_session_outcome(), acr.OUTCOME_ABANDONED)

    def test_candidate_threshold_is_120(self):
        self.assertEqual(acr.ABANDONMENT_CANDIDATE_MINUTES, 120)

    def test_terminal_boundary_is_archived(self):
        self.assertEqual(acr.TERMINAL_SESSION_STATUS, "archived")


class _FakeCursor:
    """Minimal cursor whose behaviour is driven by a shared FakeDB state."""

    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._result = []

    def execute(self, sql, params=None):
        self.db.executed.append((sql, params))
        s = " ".join(sql.split())
        self.rowcount = 0
        self._result = []
        if s.startswith("SELECT action_id, session_id, action_type FROM ai_actions"):
            self._result = [dict(r) for r in self.db.pending_actions]
        elif s.startswith("UPDATE ai_actions SET status='EXPIRED'"):
            aid = params[0]
            hit = [r for r in self.db.pending_actions if r["action_id"] == aid]
            if hit:
                self.db.pending_actions.remove(hit[0])
                self.db.expired_ids.append(aid)
                self.rowcount = 1
        elif s.startswith("SELECT s.session_id, s.customer_id, s.created_at"):
            # Anti-join: no ledger row at all, or a claim whose event never landed.
            self._result = [
                dict(r) for r in self.db.archived_sessions
                if r["session_id"] not in self.db.ledger
                or self.db.ledger[r["session_id"]]["event_id"] is None]
        elif s.startswith("INSERT IGNORE INTO ai_session_outcomes"):
            sid = params[0]
            if sid in self.db.ledger:
                self.rowcount = 0
            else:
                self.db.ledger[sid] = {"outcome": params[1], "event_id": None}
                self.rowcount = 1
        elif s.startswith("UPDATE ai_session_outcomes SET event_id"):
            eid, sid = params
            row = self.db.ledger.get(sid)
            if row and row["event_id"] is None and not self.db.fail_backfill:
                row["event_id"] = eid
                self.rowcount = 1
        elif s.startswith("SELECT event_id FROM ai_events"):
            sid = params[0]
            hit = [e for e in self.db.events
                   if e[1] == acr.EV_SESSION_OUTCOME and e[2] == sid]
            self._result = [{"event_id": hit[0][0]}] if hit else []
        elif s.startswith("INSERT INTO ai_events"):
            if self.db.fail_event_writes:
                raise RuntimeError("simulated ai_events write failure")
            self.db.events.append(params)
            self.rowcount = 1
        elif s.startswith("SELECT 1 FROM orders"):
            self.db.order_probes.append(params)
            self._result = [{"1": 1}] if self.db.truth.get("has_order") else []
        elif "event_type='agent_reply'" in s:
            self._result = [{"1": 1}] if self.db.truth.get("agent_reply") else []
        elif "event_type='session_resolved'" in s:
            self._result = [{"1": 1}] if self.db.truth.get("resolved") else []
        elif s.startswith("SELECT 1 FROM ai_events") and params and \
                acr.EV_HANDOVER_ESCALATED in params:
            self._result = [{"1": 1}] if self.db.truth.get("handover") else []
        elif s.startswith("SELECT 1 FROM ai_events") and params and \
                acr.EV_ACTION_FAILED in params:
            self._result = [{"1": 1}] if self.db.truth.get("failed") else []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeDB:
    def __init__(self):
        self.pending_actions = []
        self.expired_ids = []
        self.archived_sessions = []
        self.ledger = {}
        self.events = []
        self.executed = []
        self.truth = {}
        self.order_probes = []
        self.fail_event_writes = False
        self.fail_backfill = False

    def cursor(self):
        return _FakeCursor(self)


class ExpireActionsTests(unittest.TestCase):
    def _db(self):
        db = FakeDB()
        db.pending_actions = [
            {"action_id": "a1", "session_id": "s1", "action_type": "NAVIGATE"},
            {"action_id": "a2", "session_id": "s2", "action_type": "NAVIGATE"},
        ]
        return db

    def test_dry_run_writes_nothing(self):
        db = self._db()
        r = acr.expire_due_actions(db, dry_run=True)
        self.assertEqual(len(r["candidates"]), 2)
        self.assertEqual(len(r["expired"]), 2)  # would-expire
        self.assertEqual(db.expired_ids, [])    # nothing actually changed
        self.assertEqual(db.events, [])

    def test_live_expires_and_emits_once(self):
        db = self._db()
        r = acr.expire_due_actions(db, dry_run=False)
        self.assertEqual(len(r["expired"]), 2)
        self.assertEqual(sorted(db.expired_ids), ["a1", "a2"])
        # one ACTION_EXPIRED event per transitioned row
        self.assertEqual(len(db.events), 2)

    def test_edge_triggered_no_double_emit(self):
        db = self._db()
        acr.expire_due_actions(db, dry_run=False)
        # second run finds nothing PENDING -> no more events
        r2 = acr.expire_due_actions(db, dry_run=False)
        self.assertEqual(len(r2["expired"]), 0)
        self.assertEqual(len(db.events), 2)

    def test_failed_event_write_is_reported_not_silent(self):
        # The status transition is correct, but a lost ACTION_EXPIRED event must
        # be surfaced so the runner can log it.
        db = self._db()
        db.fail_event_writes = True
        r = acr.expire_due_actions(db, dry_run=False)
        self.assertEqual(sorted(db.expired_ids), ["a1", "a2"])
        self.assertEqual(sorted(c["action_id"] for c in r["event_failed"]),
                         ["a1", "a2"])
        self.assertEqual(db.events, [])


class FinalizeOutcomeTests(unittest.TestCase):
    def _db(self, truth):
        db = FakeDB()
        db.archived_sessions = [
            {"session_id": "s1", "customer_id": 10, "created_at": "2026-01-01",
             "last_activity": "2026-01-02"},
        ]
        db.truth = truth
        return db

    def test_dry_run_writes_nothing(self):
        db = self._db({"has_order": True})
        r = acr.finalize_archived_session_outcomes(db, dry_run=True)
        self.assertEqual(r["candidates"][0]["outcome"], acr.OUTCOME_PURCHASED)
        self.assertEqual(db.ledger, {})
        self.assertEqual(db.events, [])

    def test_live_claims_and_emits_once(self):
        db = self._db({"resolved": True})
        r = acr.finalize_archived_session_outcomes(db, dry_run=False)
        self.assertEqual(r["closed"][0]["outcome"], acr.OUTCOME_ANSWERED)
        self.assertEqual(db.ledger["s1"]["outcome"], acr.OUTCOME_ANSWERED)
        self.assertEqual(len(db.events), 1)

    def test_ledger_event_id_matches_the_stored_event(self):
        # The ledger reference must be joinable to the actual ai_events row,
        # not a locally invented uuid.
        db = self._db({})
        acr.finalize_archived_session_outcomes(db, dry_run=False)
        self.assertEqual(len(db.events), 1)
        self.assertEqual(db.ledger["s1"]["event_id"], db.events[0][0])

    def test_failed_event_write_is_retried_not_lost(self):
        db = self._db({})
        db.fail_event_writes = True
        r1 = acr.finalize_archived_session_outcomes(db, dry_run=False)
        # Claimed, but no event landed -> reported, not silently "closed".
        self.assertEqual(len(r1["closed"]), 0)
        self.assertEqual([c["session_id"] for c in r1["event_failed"]], ["s1"])
        self.assertIsNone(db.ledger["s1"]["event_id"])
        # A later run picks the session up again and completes it.
        db.fail_event_writes = False
        r2 = acr.finalize_archived_session_outcomes(db, dry_run=False)
        self.assertEqual([c["session_id"] for c in r2["closed"]], ["s1"])
        self.assertEqual(len(db.events), 1)
        self.assertEqual(db.ledger["s1"]["event_id"], db.events[0][0])

    def test_retry_after_failed_backfill_does_not_duplicate_event(self):
        # Event landed but the reference update failed: the retry must repair the
        # reference, never emit a second terminal outcome.
        db = self._db({})
        db.fail_backfill = True
        acr.finalize_archived_session_outcomes(db, dry_run=False)
        self.assertEqual(len(db.events), 1)
        self.assertIsNone(db.ledger["s1"]["event_id"])
        db.fail_backfill = False
        r2 = acr.finalize_archived_session_outcomes(db, dry_run=False)
        self.assertEqual(len(db.events), 1)          # no duplicate
        self.assertEqual(len(r2["closed"]), 0)
        self.assertEqual(db.ledger["s1"]["event_id"], db.events[0][0])

    def test_purchase_probe_is_bounded_at_both_ends(self):
        # An order must fall inside the attribution window, so one later order
        # cannot retroactively mark every earlier session PURCHASED.
        db = self._db({"has_order": True})
        acr.finalize_archived_session_outcomes(db, dry_run=True)
        self.assertTrue(db.order_probes)
        params = db.order_probes[0]
        self.assertIn("2026-01-01", params)   # lower bound: session start
        self.assertIn("2026-01-02", params)   # upper bound: last activity + horizon
        self.assertIn(acr.PURCHASE_ATTRIBUTION_HOURS, params)
        sql = [s for s, _ in db.executed if "FROM orders" in s][0]
        self.assertIn("date_created <=", " ".join(sql.split()))

    def test_idempotent_second_run_no_duplicate(self):
        db = self._db({})  # -> ABANDONED
        acr.finalize_archived_session_outcomes(db, dry_run=False)
        self.assertEqual(db.ledger["s1"]["outcome"], acr.OUTCOME_ABANDONED)
        # ledger anti-join means the session is no longer a candidate
        r2 = acr.finalize_archived_session_outcomes(db, dry_run=False)
        self.assertEqual(len(r2["candidates"]), 0)
        self.assertEqual(len(db.events), 1)

    def test_escalation_beats_resolution(self):
        db = self._db({"agent_reply": True, "resolved": True})
        r = acr.finalize_archived_session_outcomes(db, dry_run=True)
        self.assertEqual(r["candidates"][0]["outcome"], acr.OUTCOME_ESCALATED)


if __name__ == "__main__":
    unittest.main()
