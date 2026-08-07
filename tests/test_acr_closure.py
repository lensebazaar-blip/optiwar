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
            self._result = [dict(r) for r in self.db.archived_sessions
                            if r["session_id"] not in self.db.ledger]
        elif s.startswith("INSERT IGNORE INTO ai_session_outcomes"):
            sid = params[0]
            if sid in self.db.ledger:
                self.rowcount = 0
            else:
                self.db.ledger[sid] = params[1]
                self.rowcount = 1
        elif s.startswith("INSERT INTO ai_events"):
            self.db.events.append(params)
            self.rowcount = 1
        elif s.startswith("SELECT 1 FROM orders"):
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


class FinalizeOutcomeTests(unittest.TestCase):
    def _db(self, truth):
        db = FakeDB()
        db.archived_sessions = [
            {"session_id": "s1", "customer_id": 10, "created_at": "2026-01-01"},
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
        self.assertEqual(db.ledger.get("s1"), acr.OUTCOME_ANSWERED)
        self.assertEqual(len(db.events), 1)

    def test_idempotent_second_run_no_duplicate(self):
        db = self._db({})  # -> ABANDONED
        acr.finalize_archived_session_outcomes(db, dry_run=False)
        self.assertEqual(db.ledger.get("s1"), acr.OUTCOME_ABANDONED)
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
