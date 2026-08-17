"""The ACR action lifecycle against a real MariaDB, not a fake cursor.

Every other ACR test drives a recording cursor, which is right for asserting
*what SQL we send* but cannot answer the questions that decide whether a
customer's broken journey is visible:

  - ``DATE_SUB(NOW(), INTERVAL %s SECOND)`` needs a clock a fake does not have,
    so "is this confirmation still in flight?" is untestable without a server;
  - a status transition only means something if the row the next query selects
    is the row this query wrote.

Both of those were found by hand against a real database during Gate-1 rather
than by the suite. This file closes that gap, and is the reason CI runs a
database service at all.

Skipped, not failed, when no test database is reachable: a contributor without
MariaDB still gets a meaningful run.

    OPTIWAR_TEST_MYSQL_DB=optiwar2 python3 -m unittest tests.test_acr_lifecycle_mariadb
"""
import importlib.util
import json
import os
import sys
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


acr = _load("acr")
qc = _load("acr_qc")

DB_CONF = dict(
    host=os.environ.get("OPTIWAR_TEST_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("OPTIWAR_TEST_MYSQL_PORT", "3306")),
    user=os.environ.get("OPTIWAR_TEST_MYSQL_USER", "oslb6"),
    password=os.environ.get("OPTIWAR_TEST_MYSQL_PASSWORD", "testpw"),
    database=os.environ.get("OPTIWAR_TEST_MYSQL_DB", "optiwar2"),
)


def _connect():
    import pymysql
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor,
                           autocommit=True, connect_timeout=5, **DB_CONF)


def _available():
    try:
        _connect().close()
        return True
    except Exception:  # noqa: BLE001 - any failure means "no database here"
        return False


AVAILABLE = _available()


@unittest.skipUnless(AVAILABLE, "no MariaDB test database (see scripts/setup_test_db.sh)")
class ActionLifecycleOnRealMySQLTests(unittest.TestCase):
    """One confirmed navigation, and the four ways it can end."""

    @classmethod
    def setUpClass(cls):
        cls.db = _connect()
        # The schema creation path itself is worth exercising: it runs at boot on
        # a live node, and CREATE TABLE IF NOT EXISTS / index guards are exactly
        # the statements a fake cursor cannot validate.
        acr.ensure_schema(lambda: _connect())
        acr.ensure_schema(lambda: _connect())   # idempotent

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        self.session_id = "citest_" + uuid.uuid4().hex[:10]
        self.cur = self.db.cursor()
        acr.log_event(self.db, acr.EV_SESSION_STARTED, session_id=self.session_id)

    def tearDown(self):
        self.cur.execute("DELETE FROM ai_events WHERE session_id=%s", (self.session_id,))
        self.cur.execute("DELETE FROM ai_actions WHERE session_id=%s", (self.session_id,))

    # ── helpers ──

    def _confirmed_navigation(self, target="/eyeglasses/a.html", age=None):
        aid = acr.create_pending_action(self.db, self.session_id, "NAVIGATE", target)
        acr.mark_action(self.db, aid, "CONFIRMED")
        if age:
            self.cur.execute(
                "UPDATE ai_actions SET resolved_at=DATE_SUB(NOW(), INTERVAL %s SECOND)"
                " WHERE action_id=%s", (age, aid))
        return aid

    def _status(self, action_id):
        self.cur.execute("SELECT status FROM ai_actions WHERE action_id=%s", (action_id,))
        return self.cur.fetchone()["status"]

    def _expiries(self, action_id):
        self.cur.execute(
            "SELECT failure_code, payload FROM ai_events"
            " WHERE action_id=%s AND event_type=%s",
            (action_id, acr.EV_ACTION_EXPIRED))
        return self.cur.fetchall()

    def _qc(self):
        return {s["signal"]: s for s in qc.review_one(self.db, self.session_id)["signals"]}

    # ── the four endings ──

    def test_a_confirmation_inside_its_window_is_in_flight(self):
        self._confirmed_navigation()
        self.assertNotIn(qc.QC_FAILED_NAVIGATION, self._qc())

    def test_a_confirmation_past_its_window_is_a_broken_journey(self):
        self._confirmed_navigation(age=acr.EXECUTION_TTL_SECONDS + 60)
        self.assertEqual(
            self._qc()[qc.QC_FAILED_NAVIGATION]["confirmed_not_executed"], 1)

    def test_a_new_offer_ends_a_stranded_confirmation_and_says_why(self):
        old = self._confirmed_navigation(age=acr.EXECUTION_TTL_SECONDS + 60)
        new = acr.create_pending_action(self.db, self.session_id, "NAVIGATE",
                                        "/eyeglasses/b.html")
        self.assertEqual(self._status(old), "EXPIRED")
        rows = self._expiries(old)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["failure_code"], "confirmed_never_executed")
        payload = json.loads(rows[0]["payload"])
        self.assertEqual(payload["from_status"], "CONFIRMED")
        self.assertEqual(payload["superseded_by"], new)
        # Still reported: the terminal state must not erase the defect.
        self.assertEqual(
            self._qc()[qc.QC_FAILED_NAVIGATION]["confirmed_not_executed"], 1)

    def test_a_new_offer_leaves_a_live_confirmation_for_the_sweep(self):
        # The customer said yes and typed again seconds later, before the page
        # could report arrival. Superseding the row here would destroy the only
        # evidence, so it is left for the sweep to judge at the offer deadline.
        old = self._confirmed_navigation()
        acr.create_pending_action(self.db, self.session_id, "NAVIGATE",
                                  "/eyeglasses/b.html")
        self.assertEqual(self._status(old), "CONFIRMED")
        self.assertEqual(self._expiries(old), ())
        self.assertNotIn(qc.QC_FAILED_NAVIGATION, self._qc())

        self.cur.execute(
            "UPDATE ai_actions SET expires_at=DATE_SUB(NOW(), INTERVAL 1 SECOND)"
            " WHERE action_id=%s", (old,))
        acr.expire_due_actions(self.db, dry_run=False)
        self.assertEqual(self._status(old), "EXPIRED")
        self.assertEqual(self._expiries(old)[0]["failure_code"],
                         "confirmed_never_executed")
        self.assertEqual(
            self._qc()[qc.QC_FAILED_NAVIGATION]["confirmed_not_executed"], 1)

    def test_an_unanswered_offer_is_a_conversion_miss_not_a_defect(self):
        aid = acr.create_pending_action(self.db, self.session_id, "NAVIGATE",
                                        "/eyeglasses/a.html")
        acr.create_pending_action(self.db, self.session_id, "NAVIGATE",
                                  "/eyeglasses/b.html")
        self.assertEqual(self._status(aid), "SUPERSEDED")
        self.assertEqual(self._expiries(aid), ())
        self.assertNotIn(qc.QC_FAILED_NAVIGATION, self._qc())

    def test_an_executed_navigation_completes_the_journey(self):
        aid = self._confirmed_navigation()
        acr.mark_action(self.db, aid, "EXECUTED", result_code="arrived")
        acr.log_event(self.db, acr.EV_ACTION_EXECUTED, session_id=self.session_id,
                      action_id=aid, action_type="NAVIGATE", success=True)
        self.assertEqual(self._status(aid), "EXECUTED")
        signals = self._qc()
        self.assertIn(qc.QC_JOURNEY_COMPLETED, signals)
        self.assertNotIn(qc.QC_FAILED_NAVIGATION, signals)

    def test_a_dry_run_sweep_writes_nothing_to_a_real_database(self):
        # The promise Step-5 evidence rests on, checked where it can actually be
        # broken: a live server, real DDL/DML permissions, real rows.
        old = self._confirmed_navigation(age=acr.EXECUTION_TTL_SECONDS + 60)
        self.cur.execute(
            "UPDATE ai_actions SET expires_at=DATE_SUB(NOW(), INTERVAL 1 SECOND)"
            " WHERE action_id=%s", (old,))
        before = self._status(old)
        acr.expire_due_actions(self.db, dry_run=True)
        self.assertEqual(self._status(old), before)
        self.assertEqual(self._expiries(old), ())


if __name__ == "__main__":
    unittest.main()
