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


T, F, U = acr.TRUTH_TRUE, acr.TRUTH_FALSE, acr.TRUTH_UNKNOWN


class DecideOutcomeTests(unittest.TestCase):
    def _decide(self, **kw):
        # unittest.TestCase reserves the instance attribute ``_outcome``.
        outcome, reason = acr.decide_session_outcome(**kw)
        self.assertIsNone(reason)
        return outcome

    def test_priority_order(self):
        # ESCALATED beats FAILED/ANSWERED.
        self.assertEqual(self._decide(is_escalated=T, is_failed=T,
                                      normally_resolved=T),
                         acr.OUTCOME_ESCALATED)
        # FAILED beats ANSWERED/ABANDONED.
        self.assertEqual(self._decide(is_failed=T, normally_resolved=T),
                         acr.OUTCOME_FAILED)
        # Normal close with no higher truth -> ANSWERED.
        self.assertEqual(self._decide(normally_resolved=T),
                         acr.OUTCOME_ANSWERED)
        # Archived, nothing else -> ABANDONED (matured candidate).
        self.assertEqual(self._decide(), acr.OUTCOME_ABANDONED)

    def test_purchase_is_not_a_conversation_outcome(self):
        """A purchase is commerce attribution, not how the chat ended."""
        self.assertNotIn("PURCHASED", acr._OUTCOME_PRIORITY)
        self.assertFalse(hasattr(acr, "OUTCOME_PURCHASED"))

    def test_candidate_threshold_is_120(self):
        self.assertEqual(acr.ABANDONMENT_CANDIDATE_MINUTES, 120)

    def test_terminal_boundary_is_archived(self):
        self.assertEqual(acr.TERMINAL_SESSION_STATUS, "archived")


class TriStateTruthTests(unittest.TestCase):
    """UNKNOWN must defer the outcome, never collapse into FALSE."""

    def test_unknown_escalation_defers(self):
        outcome, reason = acr.decide_session_outcome(is_escalated=U)
        self.assertIsNone(outcome)
        self.assertEqual(reason, acr.DEFER_ESCALATION_TRUTH)

    def test_unknown_failure_defers(self):
        outcome, reason = acr.decide_session_outcome(is_escalated=F, is_failed=U)
        self.assertIsNone(outcome)
        self.assertEqual(reason, acr.DEFER_FAILURE_TRUTH)

    def test_unknown_resolution_defers_answered_vs_abandoned(self):
        outcome, reason = acr.decide_session_outcome(
            is_escalated=F, is_failed=F, normally_resolved=U)
        self.assertIsNone(outcome)
        self.assertEqual(reason, acr.DEFER_RESOLUTION_TRUTH)

    def test_established_higher_truth_wins_over_unknown_lower_truth(self):
        """No value of a lower probe could change ESCALATED, so don't stall."""
        outcome, reason = acr.decide_session_outcome(
            is_escalated=T, is_failed=U, normally_resolved=U)
        self.assertEqual(outcome, acr.OUTCOME_ESCALATED)
        self.assertIsNone(reason)

    def test_truth_or_semantics(self):
        self.assertEqual(acr.truth_or(T, U), T)     # proof beats ignorance
        self.assertEqual(acr.truth_or(F, U), U)     # ignorance beats absence
        self.assertEqual(acr.truth_or(F, F), F)
        self.assertEqual(acr.truth_or(), F)

    def test_failed_query_probes_unknown_not_false(self):
        class Exploding:
            def cursor(self):
                raise RuntimeError("table 'orders' doesn't exist")
        self.assertEqual(acr._probe(Exploding(), "SELECT 1", ()), U)
        self.assertEqual(acr._probe_value(Exploding(), "SELECT 1", ()), (U, None))


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
        elif (s.startswith("SELECT s.session_id, s.customer_id, s.created_at")
              and "ai_session_commerce" in s):
            self._result = [
                dict(r) for r in self.db.archived_sessions
                if r["session_id"] not in self.db.commerce
                or self.db.commerce[r["session_id"]]["event_id"] is None]
        elif s.startswith("SELECT s.session_id, s.customer_id, s.created_at"):
            # Anti-join: no ledger row at all, or a claim whose event never landed.
            self._result = [
                dict(r) for r in self.db.archived_sessions
                if r["session_id"] not in self.db.ledger
                or self.db.ledger[r["session_id"]]["event_id"] is None]
        elif s.startswith("INSERT IGNORE INTO ai_session_commerce"):
            sid = params[0]
            if sid in self.db.commerce:
                self.rowcount = 0
            else:
                self.db.commerce[sid] = {"order_id": params[1],
                                         "attribution_type": params[2],
                                         "attribution_window_hours": params[3],
                                         "event_id": None}
                self.rowcount = 1
        elif s.startswith("UPDATE ai_session_commerce SET event_id"):
            eid, sid = params
            row = self.db.commerce.get(sid)
            if row and row["event_id"] is None and not self.db.fail_backfill:
                row["event_id"] = eid
                self.rowcount = 1
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
        elif s.startswith("SELECT COUNT(*) FROM information_schema.TABLES"):
            self._result = [{"c": 1 if params[0] in self.db.column_collation
                             else 0}]
        elif s.startswith("SELECT COLLATION_NAME FROM information_schema.COLUMNS"):
            coll = self.db.column_collation.get(params[0])
            self._result = [{"COLLATION_NAME": coll}] if coll else []
        elif s.startswith("ALTER TABLE") and self.db.deny_alter:
            raise RuntimeError("ALTER command denied to user")
        elif s.startswith("SELECT event_id FROM ai_events"):
            sid, etype = params
            hit = [e for e in self.db.events if e[1] == etype and e[2] == sid]
            self._result = [{"event_id": hit[0][0]}] if hit else []
        elif s.startswith("INSERT INTO ai_events"):
            if self.db.fail_event_writes:
                raise RuntimeError("simulated ai_events write failure")
            self.db.events.append(params)
            self.rowcount = 1
        elif s.startswith("SELECT order_id FROM orders"):
            self._deny_if_unreadable("orders")
            self.db.order_probes.append(params)
            oid = self.db.truth.get("order_id")
            self._result = [{"order_id": oid}] if oid else []
        elif "event_type='agent_reply'" in s:
            self._deny_if_unreadable("chat_events")
            self._result = [{"1": 1}] if self.db.truth.get("agent_reply") else []
        elif "event_type='session_resolved'" in s:
            self._deny_if_unreadable("chat_events")
            self._result = [{"1": 1}] if self.db.truth.get("resolved") else []
        elif s.startswith("SELECT MAX(created_at) FROM ai_events"):
            # 'failure_probe' denies only this read, so a test can make the
            # failure signal unknown while escalation truth is known.
            self._deny_if_unreadable("ai_events", "failure_probe")
            at = self.db.truth.get("last_failure_at")
            self._result = [{"m": at}] if at else [{"m": None}]
        elif "created_at > %s" in s:
            self._deny_if_unreadable("ai_events", "failure_probe")
            self._result = [{"1": 1}] if self.db.truth.get("recovered") else []
        elif s.startswith("SELECT 1 FROM ai_events") and params and \
                acr.EV_HANDOVER_ESCALATED in params:
            self._deny_if_unreadable("ai_events")
            self._result = [{"1": 1}] if self.db.truth.get("handover") else []
        elif s.startswith("SELECT 1 FROM ai_events") and params and \
                acr.EV_OUTCOME_DEFERRED in params:
            # Has this session already been recorded as deferred for this
            # reason? (failure_code is column index 8 of the ai_events insert.)
            sid, etype, reason = params
            self._result = [{"1": 1} for e in self.db.events
                            if e[1] == etype and e[2] == sid and e[8] == reason][:1]

    def _deny_if_unreadable(self, *tables):
        """Simulate a denied grant / missing table for one truth source."""
        for table in tables:
            if table in self.db.unreadable:
                raise RuntimeError("SELECT command denied on '%s'" % table)

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
        self.commerce = {}
        self.events = []
        self.executed = []
        self.truth = {}
        self.order_probes = []
        self.unreadable = set()
        self.fail_event_writes = False
        self.fail_backfill = False
        # table -> session_id collation; absent means the table does not exist.
        self.column_collation = {}
        self.deny_alter = False

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
        db = self._db({"resolved": True})
        r = acr.finalize_archived_session_outcomes(db, dry_run=True)
        self.assertEqual(r["candidates"][0]["outcome"], acr.OUTCOME_ANSWERED)
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

    def test_outcome_sweep_never_touches_the_orders_table(self):
        """Conversation outcome must not depend on commerce truth at all."""
        db = self._db({"order_id": "ORD-1"})
        acr.finalize_archived_session_outcomes(db, dry_run=True)
        self.assertEqual(db.order_probes, [])
        self.assertFalse([s for s, _ in db.executed if "FROM orders" in s])

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


class DeferredOutcomeTests(unittest.TestCase):
    """An unreadable truth source must produce no immutable outcome at all."""

    def _db(self, truth, unreadable=()):
        db = FakeDB()
        db.archived_sessions = [
            {"session_id": "s1", "customer_id": 10, "created_at": "2026-01-01",
             "last_activity": "2026-01-02"},
        ]
        db.truth = truth
        db.unreadable = set(unreadable)
        return db

    def test_unreadable_escalation_source_defers_instead_of_abandoning(self):
        db = self._db({}, unreadable=["chat_events", "ai_events"])
        r = acr.finalize_archived_session_outcomes(db, dry_run=False)
        self.assertEqual(r["candidates"], [])
        self.assertEqual(r["deferred"][0]["session_id"], "s1")
        self.assertEqual(r["deferred"][0]["reason"], acr.DEFER_ESCALATION_TRUTH)
        self.assertEqual(db.ledger, {})   # nothing immutable written

    def test_deferral_emits_outcome_deferred_with_a_reason(self):
        db = self._db({}, unreadable=["chat_events", "ai_events"])
        acr.finalize_archived_session_outcomes(db, dry_run=False)
        kinds = [e[1] for e in db.events]
        self.assertEqual(kinds, [acr.EV_OUTCOME_DEFERRED])
        self.assertIn(acr.DEFER_ESCALATION_TRUTH, db.events[0])

    def test_deferred_session_is_retried_on_the_next_sweep(self):
        db = self._db({}, unreadable=["chat_events", "ai_events"])
        acr.finalize_archived_session_outcomes(db, dry_run=False)
        self.assertEqual(db.ledger, {})
        db.unreadable = set()   # grant restored
        r2 = acr.finalize_archived_session_outcomes(db, dry_run=False)
        self.assertEqual(r2["closed"][0]["outcome"], acr.OUTCOME_ABANDONED)
        self.assertEqual(db.ledger["s1"]["outcome"], acr.OUTCOME_ABANDONED)

    def test_dry_run_reports_deferrals_without_writing(self):
        db = self._db({}, unreadable=["chat_events", "ai_events"])
        r = acr.finalize_archived_session_outcomes(db, dry_run=True)
        self.assertEqual(len(r["deferred"]), 1)
        self.assertEqual(db.events, [])
        self.assertEqual(db.ledger, {})

    def test_persistent_deferral_is_recorded_once_not_once_per_sweep(self):
        """A */15 cron must not write 96 identical events a day per session."""
        db = self._db({}, unreadable=["chat_events", "ai_events"])
        for _ in range(5):
            r = acr.finalize_archived_session_outcomes(db, dry_run=False)
            self.assertEqual(len(r["deferred"]), 1)  # still reported every run
        self.assertEqual(len(db.events), 1)

    def test_a_new_deferral_reason_is_recorded_separately(self):
        db = self._db({}, unreadable=["chat_events", "ai_events"])
        acr.finalize_archived_session_outcomes(db, dry_run=False)
        # Escalation truth returns; the failure probe is now the blocker.
        db.unreadable = {"failure_probe"}
        acr.finalize_archived_session_outcomes(db, dry_run=False)
        reasons = {e[8] for e in db.events}
        self.assertEqual(reasons,
                         {acr.DEFER_ESCALATION_TRUTH, acr.DEFER_FAILURE_TRUTH})


class TerminalFailureTests(unittest.TestCase):
    """FAILED means terminal *unrecovered* failure, not any past failure."""

    def _db(self, truth, unreadable=()):
        db = FakeDB()
        db.archived_sessions = [
            {"session_id": "s1", "customer_id": 10, "created_at": "2026-01-01",
             "last_activity": "2026-01-02"},
        ]
        db.truth = truth
        db.unreadable = set(unreadable)
        return db

    def test_no_failure_at_all_is_false(self):
        db = self._db({})
        self.assertEqual(acr.terminal_failure_truth(db, "s1"), F)

    def test_failure_followed_by_success_is_recovered(self):
        """Provider timeout -> retry -> recommendation is not a failed chat."""
        db = self._db({"last_failure_at": "2026-01-01 10:00:00",
                       "recovered": True})
        self.assertEqual(acr.terminal_failure_truth(db, "s1"), F)

    def test_failure_with_nothing_after_it_is_terminal(self):
        db = self._db({"last_failure_at": "2026-01-01 10:00:00",
                       "recovered": False})
        self.assertEqual(acr.terminal_failure_truth(db, "s1"), T)

    def test_unreadable_event_stream_is_unknown(self):
        db = self._db({"last_failure_at": "2026-01-01 10:00:00"},
                      unreadable=["ai_events"])
        self.assertEqual(acr.terminal_failure_truth(db, "s1"), U)

    def test_recovered_session_is_answered_not_failed(self):
        db = self._db({"last_failure_at": "2026-01-01 10:00:00",
                       "recovered": True, "resolved": True})
        r = acr.finalize_archived_session_outcomes(db, dry_run=True)
        self.assertEqual(r["candidates"][0]["outcome"], acr.OUTCOME_ANSWERED)

    def test_unrecovered_session_is_failed(self):
        db = self._db({"last_failure_at": "2026-01-01 10:00:00",
                       "recovered": False, "resolved": True})
        r = acr.finalize_archived_session_outcomes(db, dry_run=True)
        self.assertEqual(r["candidates"][0]["outcome"], acr.OUTCOME_FAILED)


class CommerceAttributionTests(unittest.TestCase):
    """Purchase is recorded separately, with its basis, and never rewrites history."""

    def _db(self, truth, unreadable=()):
        db = FakeDB()
        db.archived_sessions = [
            {"session_id": "s1", "customer_id": 10, "created_at": "2026-01-01",
             "last_activity": "2026-01-02"},
        ]
        db.truth = truth
        db.unreadable = set(unreadable)
        return db

    def test_attribution_records_order_id_type_and_window(self):
        db = self._db({"order_id": "ORD-1"})
        r = acr.attribute_archived_session_commerce(db, dry_run=False)
        row = db.commerce["s1"]
        self.assertEqual(row["order_id"], "ORD-1")
        self.assertEqual(row["attribution_type"], acr.ATTRIBUTION_SESSION_WINDOW)
        self.assertEqual(row["attribution_window_hours"],
                         acr.PURCHASE_ATTRIBUTION_HOURS)
        self.assertEqual([e[1] for e in db.events], [acr.EV_COMMERCE_OUTCOME])
        self.assertEqual(r["attributed"][0]["order_id"], "ORD-1")

    def test_attribution_window_is_bounded_at_both_ends(self):
        # Without an upper bound one later order would attribute to every
        # earlier session that shopper ever had.
        db = self._db({"order_id": "ORD-1"})
        acr.attribute_archived_session_commerce(db, dry_run=True)
        params = db.order_probes[0]
        self.assertIn("2026-01-01", params)   # lower bound: session start
        self.assertIn("2026-01-02", params)   # upper bound: last activity + horizon
        self.assertIn(acr.PURCHASE_ATTRIBUTION_HOURS, params)
        sql = [s for s, _ in db.executed if "FROM orders" in s][0]
        self.assertIn("date_created <=", " ".join(sql.split()))

    def test_purchase_does_not_change_the_conversation_outcome(self):
        """The chat was answered; the purchase is a separate, additional fact."""
        db = self._db({"order_id": "ORD-1", "resolved": True})
        outcome = acr.finalize_archived_session_outcomes(db, dry_run=False)
        acr.attribute_archived_session_commerce(db, dry_run=False)
        self.assertEqual(db.ledger["s1"]["outcome"], acr.OUTCOME_ANSWERED)
        self.assertEqual(db.commerce["s1"]["order_id"], "ORD-1")
        self.assertEqual(outcome["closed"][0]["outcome"], acr.OUTCOME_ANSWERED)

    def test_order_arriving_after_archival_needs_no_outcome_rewrite(self):
        # Conversation closes first; the order lands later. The immutable
        # outcome is untouched and attribution still records the purchase.
        db = self._db({"resolved": True})
        acr.finalize_archived_session_outcomes(db, dry_run=False)
        self.assertEqual(db.ledger["s1"]["outcome"], acr.OUTCOME_ANSWERED)
        db.truth["order_id"] = "ORD-LATER"
        acr.attribute_archived_session_commerce(db, dry_run=False)
        self.assertEqual(db.ledger["s1"]["outcome"], acr.OUTCOME_ANSWERED)
        self.assertEqual(db.commerce["s1"]["order_id"], "ORD-LATER")

    def test_no_order_writes_nothing(self):
        db = self._db({})
        r = acr.attribute_archived_session_commerce(db, dry_run=False)
        self.assertEqual(r["attributed"], [])
        self.assertEqual(db.commerce, {})
        self.assertEqual(db.events, [])

    def test_unreadable_orders_defers_rather_than_recording_no_purchase(self):
        db = self._db({"order_id": "ORD-1"}, unreadable=["orders"])
        r = acr.attribute_archived_session_commerce(db, dry_run=False)
        self.assertEqual(r["deferred"][0]["reason"], acr.DEFER_ORDER_TRUTH)
        self.assertEqual(db.commerce, {})
        self.assertEqual([e[1] for e in db.events], [acr.EV_OUTCOME_DEFERRED])

    def test_dry_run_writes_nothing(self):
        db = self._db({"order_id": "ORD-1"})
        r = acr.attribute_archived_session_commerce(db, dry_run=True)
        self.assertEqual(len(r["candidates"]), 1)
        self.assertEqual(db.commerce, {})
        self.assertEqual(db.events, [])

    def test_idempotent_second_run_no_duplicate(self):
        db = self._db({"order_id": "ORD-1"})
        acr.attribute_archived_session_commerce(db, dry_run=False)
        r2 = acr.attribute_archived_session_commerce(db, dry_run=False)
        self.assertEqual(r2["attributed"], [])
        self.assertEqual(len(db.events), 1)

    def test_failed_event_write_is_retried_not_lost(self):
        db = self._db({"order_id": "ORD-1"})
        db.fail_event_writes = True
        r1 = acr.attribute_archived_session_commerce(db, dry_run=False)
        self.assertEqual(r1["attributed"], [])
        self.assertEqual([c["session_id"] for c in r1["event_failed"]], ["s1"])
        self.assertIsNone(db.commerce["s1"]["event_id"])
        db.fail_event_writes = False
        r2 = acr.attribute_archived_session_commerce(db, dry_run=False)
        self.assertEqual([c["session_id"] for c in r2["attributed"]], ["s1"])
        self.assertEqual(db.commerce["s1"]["event_id"], db.events[0][0])

    def test_event_id_is_joinable_to_the_stored_event(self):
        db = self._db({"order_id": "ORD-1"})
        acr.attribute_archived_session_commerce(db, dry_run=False)
        self.assertEqual(db.commerce["s1"]["event_id"], db.events[0][0])


class LedgerAntiJoinCollationTests(unittest.TestCase):
    """chat_sessions and the ledger tables need not share a collation — on
    production they do not, and an unqualified comparison between them is an
    error (1267), not a mismatch. Both anti-joins must therefore say which
    collation the comparison uses."""

    def _join_sql(self, finder):
        db = FakeDB()
        finder(db)
        joins = [sql for sql, _p in db.executed if "LEFT JOIN" in sql]
        self.assertEqual(len(joins), 1)
        return " ".join(joins[0].split())

    def test_outcome_anti_join_states_its_collation(self):
        sql = self._join_sql(acr.find_sessions_awaiting_outcome)
        self.assertIn("ai_session_outcomes", sql)
        self.assertIn("s.session_id COLLATE utf8mb4_general_ci", sql)

    def test_commerce_anti_join_states_its_collation(self):
        sql = self._join_sql(acr.find_sessions_awaiting_attribution)
        self.assertIn("ai_session_commerce", sql)
        self.assertIn("s.session_id COLLATE utf8mb4_general_ci", sql)

    def test_ledger_ddl_pins_the_same_collation(self):
        # Otherwise the tables inherit the server default, which differs between
        # MySQL 5.7 and 8.0, and a new node would reintroduce the mismatch.
        db = FakeDB()
        acr.ensure_closure_schema(lambda: _ClosingDB(db))
        ddl = [" ".join(s.split()) for s, _p in db.executed
               if s.strip().startswith("CREATE TABLE")]
        self.assertEqual(len(ddl), 2)
        for stmt in ddl:
            self.assertIn("session_id VARCHAR(64) COLLATE utf8mb4_general_ci",
                          " ".join(stmt.split()))


class LedgerCollationAlignmentTests(unittest.TestCase):
    """A ledger created before the collation was stated must be converted.

    CREATE TABLE IF NOT EXISTS is a no-op on those installations, so without
    this the anti-joins keep raising 'illegal mix of collations' and no outcome
    or attribution is ever recorded — the exact production failure, surviving
    the fix."""

    def _alters(self, db):
        return [" ".join(s.split()) for s, _p in db.executed
                if s.strip().startswith("ALTER TABLE")]

    def test_existing_ledger_in_another_collation_is_converted(self):
        db = FakeDB()
        db.column_collation = {"ai_session_outcomes": "utf8mb4_unicode_ci",
                               "ai_session_commerce": "utf8mb4_unicode_ci"}
        acr.ensure_closure_schema(lambda: _ClosingDB(db))
        alters = self._alters(db)
        self.assertEqual(len(alters), 2)
        for stmt in alters:
            self.assertIn("MODIFY session_id VARCHAR(64) COLLATE "
                          "utf8mb4_general_ci NOT NULL", stmt)

    def test_a_correct_table_is_never_rewritten(self):
        # ALTER on a large ledger is not free; only a genuine mismatch earns it.
        db = FakeDB()
        db.column_collation = {"ai_session_outcomes": acr.LEDGER_COLLATION,
                               "ai_session_commerce": acr.LEDGER_COLLATION}
        acr.ensure_closure_schema(lambda: _ClosingDB(db))
        self.assertEqual(self._alters(db), [])

    def test_only_the_mismatched_table_is_altered(self):
        db = FakeDB()
        db.column_collation = {"ai_session_outcomes": acr.LEDGER_COLLATION,
                               "ai_session_commerce": "utf8mb4_unicode_ci"}
        acr.ensure_closure_schema(lambda: _ClosingDB(db))
        alters = self._alters(db)
        self.assertEqual(len(alters), 1)
        self.assertIn("ai_session_commerce", alters[0])

    def test_absent_tables_are_not_altered(self):
        # Freshly created by the CREATE above; information_schema has no row for
        # them in this fake, and inventing an ALTER would fail on a real box.
        db = FakeDB()
        acr.ensure_closure_schema(lambda: _ClosingDB(db))
        self.assertEqual(self._alters(db), [])

    def test_a_dry_run_reports_the_mismatch_and_writes_nothing(self):
        # The ALTER rebuilds a primary key. A preview run that promises to change
        # nothing must not be the thing that rewrites a production table.
        db = FakeDB()
        db.column_collation = {"ai_session_outcomes": "utf8mb4_unicode_ci",
                               "ai_session_commerce": "utf8mb4_unicode_ci"}
        pending = acr.ensure_closure_schema(lambda: _ClosingDB(db),
                                            allow_ddl=False)
        self.assertEqual(len(pending), 2)
        for item in pending:
            self.assertIn("needs utf8mb4_general_ci", item)
        written = [s for s, _p in db.executed
                   if s.strip().startswith(("ALTER TABLE", "CREATE TABLE"))]
        self.assertEqual(written, [])

    def test_a_dry_run_reports_a_missing_ledger_instead_of_creating_it(self):
        db = FakeDB()          # no table exists in this fake
        pending = acr.ensure_closure_schema(lambda: _ClosingDB(db),
                                            allow_ddl=False)
        self.assertEqual(len(pending), 2)
        for item in pending:
            self.assertIn("needs CREATE", item)
        self.assertEqual([s for s, _p in db.executed
                          if s.strip().startswith("CREATE TABLE")], [])

    def test_a_live_run_returns_what_it_applied(self):
        db = FakeDB()
        db.column_collation = {"ai_session_outcomes": "utf8mb4_unicode_ci",
                               "ai_session_commerce": acr.LEDGER_COLLATION}
        applied = acr.ensure_closure_schema(lambda: _ClosingDB(db))
        self.assertEqual(len(applied), 1)
        self.assertIn("ai_session_outcomes", applied[0])

    def test_a_denied_alter_does_not_break_boot(self):
        # ensure_closure_schema is called on the event path; a missing ALTER
        # grant must degrade to the previous behaviour, not raise.
        db = FakeDB()
        db.column_collation = {"ai_session_outcomes": "utf8mb4_unicode_ci",
                               "ai_session_commerce": "utf8mb4_unicode_ci"}
        db.deny_alter = True
        acr.ensure_closure_schema(lambda: _ClosingDB(db))


class _ClosingDB:
    """ensure_closure_schema owns the connection it is given and closes it."""

    def __init__(self, db):
        self._db = db

    def cursor(self):
        return self._db.cursor()

    def close(self):
        pass


if __name__ == "__main__":
    unittest.main()
