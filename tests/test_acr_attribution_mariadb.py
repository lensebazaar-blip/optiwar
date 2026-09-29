"""The decided attribution rule, evaluated by a real database.

    One order -> at most one attributable AI session -> nearest eligible
    preceding session.

That rule lives in a ``NOT EXISTS`` correlated against ``chat_sessions``, and a
fake cursor cannot evaluate it: it can only confirm the SQL was sent. So the
question that actually matters to a revenue figure — *which* session gets the
credit when a shopper had three conversations and bought once — is only really
answered here, against MariaDB, on rows this test inserts.

The fixtures are two customers, several sessions and several orders, because the
failure mode of the previous rule (one order credited to every preceding
session) only appears with more than one session.

Skipped, not failed, when no test database is reachable.

    OPTIWAR_TEST_MYSQL_DB=optiwar2 python3 -m unittest \\
        tests.test_acr_attribution_mariadb
"""
import importlib.util
import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


acr = _load("acr")

DB_CONF = dict(
    host=os.environ.get("OPTIWAR_TEST_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("OPTIWAR_TEST_MYSQL_PORT", "3306")),
    user=os.environ.get("OPTIWAR_TEST_MYSQL_USER", "oslb6"),
    password=os.environ.get("OPTIWAR_TEST_MYSQL_PASSWORD", "testpw"),
    database=os.environ.get("OPTIWAR_TEST_MYSQL_DB", "optiwar2"),
)

# Only the columns attribution reads. Created on a test database when absent —
# never altered if it already exists, so pointing this at a database that has a
# real orders table exercises the real one.
ORDERS_DDL = """CREATE TABLE IF NOT EXISTS orders (
    order_id     VARCHAR(64) PRIMARY KEY,
    customer_id  BIGINT NULL,
    is_test      TINYINT NOT NULL DEFAULT 0,
    date_created DATETIME NULL,
    KEY idx_customer (customer_id, date_created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

# Likewise for the conversation table the rule correlates against. CI starts
# from an empty database, where every one of these tests failed on a missing
# chat_sessions until this existed; the collation is stated because the anti-join
# in acr compares session ids across tables (see #31/#34).
CHAT_SESSIONS_DDL = """CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id       VARCHAR(64) NOT NULL PRIMARY KEY,
    customer_id      BIGINT NULL,
    contact_email    VARCHAR(255) NULL,
    contact_name     VARCHAR(255) NULL,
    status           VARCHAR(24) NOT NULL DEFAULT 'active',
    current_page_url TEXT NULL,
    created_at       DATETIME NULL,
    last_activity    DATETIME NULL,
    resolved_at      DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci"""

# The gateway's record of money. Attribution reads only order_id and status.
PAYMENT_COLLECTOR_DDL = """CREATE TABLE IF NOT EXISTS payment_collector (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    order_id     VARCHAR(255) NOT NULL,
    payment_ref  VARCHAR(191) NULL,
    payment_dump LONGTEXT NOT NULL,
    status       ENUM('TXN_SUCCESS','failed') NULL DEFAULT 'failed',
    date_created DATETIME NULL,
    KEY idx_order (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

BASE = datetime(2026, 6, 1, 9, 0, 0)

# An unauthenticated shopper. A sentinel rather than None, because None is the
# helpers' "use this test's customer" default and a guest fixture that silently
# became an authenticated one would make the guest test pass for the wrong
# reason.
GUEST = object()


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
class NearestPrecedingAttributionTests(unittest.TestCase):
    """Which session earns the credit, decided by SQL rather than by intent."""

    @classmethod
    def setUpClass(cls):
        cls.db = _connect()
        cur = cls.db.cursor()
        cur.execute(ORDERS_DDL)
        cur.execute(CHAT_SESSIONS_DDL)
        cur.execute(PAYMENT_COLLECTOR_DDL)
        # Both, in order: ensure_schema owns ai_actions/ai_events and
        # ensure_closure_schema owns the ledgers. On a database that already has
        # them this is a no-op; on CI's empty one it is the difference between
        # twelve tests and twelve 1146s.
        acr.ensure_schema(lambda: _connect())
        acr.ensure_closure_schema(lambda: _connect())

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        self.tag = uuid.uuid4().hex[:8]
        # Customer ids are namespaced per test so a shared database cannot make
        # one test's shopper appear in another's window.
        self.customer = 900000 + int(self.tag[:4], 16)
        self.other = self.customer + 1
        self.cur = self.db.cursor()
        self.sessions = []
        self.orders = []

    def tearDown(self):
        for oid in self.orders:
            self.cur.execute("DELETE FROM payment_collector WHERE order_id=%s", (oid,))
        for sid in self.sessions:
            self.cur.execute("DELETE FROM ai_events WHERE session_id=%s", (sid,))
            self.cur.execute("DELETE FROM ai_session_commerce WHERE session_id=%s",
                             (sid,))
            self.cur.execute("DELETE FROM ai_session_outcomes WHERE session_id=%s",
                             (sid,))
            self.cur.execute("DELETE FROM chat_sessions WHERE session_id=%s", (sid,))
        self.cur.execute("DELETE FROM orders WHERE customer_id IN (%s,%s)",
                         (self.customer, self.other))

    # ── fixtures ──

    def _session(self, minutes, customer=None, active_minutes=30,
                 status=acr.TERMINAL_SESSION_STATUS, session_id=None):
        """A chat session starting ``minutes`` after BASE."""
        sid = session_id or ("attr_%s_%d" % (self.tag, minutes))
        start = BASE + timedelta(minutes=minutes)
        if customer is None:
            customer = self.customer
        elif customer is GUEST:
            customer = None
        self.cur.execute(
            """INSERT INTO chat_sessions
                 (session_id, customer_id, status, created_at, last_activity)
               VALUES (%s,%s,%s,%s,%s)""",
            (sid, customer, status, start,
             start + timedelta(minutes=active_minutes)))
        self.sessions.append(sid)
        return sid

    def _order(self, minutes, customer=None, is_test=0, order_id=None,
               payment='TXN_SUCCESS'):
        """An order, paid at the gateway unless ``payment`` says otherwise:
        ``'failed'`` for a declined attempt, ``None`` for an order the gateway
        never answered for (pending, or merely created)."""
        oid = order_id or ("ORD_%s_%d" % (self.tag, minutes))
        self.cur.execute(
            """INSERT INTO orders (order_id, customer_id, is_test, date_created)
               VALUES (%s,%s,%s,%s)""",
            (oid, self.customer if customer is None else customer, is_test,
             BASE + timedelta(minutes=minutes)))
        self.orders.append(oid)
        if payment is not None:
            self.cur.execute(
                """INSERT INTO payment_collector (order_id, payment_dump, status)
                   VALUES (%s, %s, %s)""", (oid, '{}', payment))
        return oid

    def _attributed(self):
        """{session_id: order_id} as the live sweep records it, this test only.

        The sweep is database-wide by design, so its result is filtered to the
        sessions this test created. Without that, rows another test (or an
        earlier interrupted run) left behind read as this test's own
        attributions — which is precisely how a green suite hides a rule that
        credits the wrong session.
        """
        result = acr.attribute_archived_session_commerce(self.db, dry_run=False)
        mine = set(self.sessions)
        self.assertEqual([c for c in result["already_claimed"]
                          if c.get("session_id") in mine],
                         [], "one order, two sessions")
        return {c["session_id"]: c["order_id"] for c in result["attributed"]
                if c["session_id"] in mine}

    # ── the rule ──

    def test_the_nearest_preceding_session_takes_the_credit(self):
        # Three conversations, one purchase. The old rule credited the order to
        # all three; 29 sessions and 5 orders produced 18 claims on one order.
        early = self._session(0)
        middle = self._session(60)
        nearest = self._session(120)
        order = self._order(150)
        self.assertEqual(self._attributed(), {nearest: order})
        for sid in (early, middle):
            self.assertNotIn(sid, self._attributed())

    def test_a_session_starting_after_the_order_cannot_have_caused_it(self):
        before = self._session(0)
        self._session(200)          # started after the purchase
        order = self._order(100)
        self.assertEqual(self._attributed(), {before: order})

    def test_one_session_may_assist_several_orders(self):
        # The asymmetry the rule states: an order has at most one session, a
        # session may have several orders. It claims the earliest here; the rest
        # remain available to a later session that precedes them.
        only = self._session(0, active_minutes=600)
        first = self._order(60)
        self._order(120)
        self.assertEqual(self._attributed(), {only: first})

    def test_another_shopper_is_never_credited(self):
        mine = self._session(0)
        theirs = self._session(30, customer=self.other)
        order = self._order(60)
        got = self._attributed()
        self.assertEqual(got, {mine: order})
        self.assertNotIn(theirs, got)

    def test_a_guest_session_is_left_unattributed(self):
        guest = self._session(0, customer=GUEST)
        self._order(60)
        self.assertEqual(self._attributed(), {})
        self.cur.execute("SELECT COUNT(*) AS n FROM ai_session_commerce"
                         " WHERE session_id=%s", (guest,))
        self.assertEqual(self.cur.fetchone()["n"], 0)

    def test_an_order_beyond_the_eligibility_ceiling_is_unattributed(self):
        # The ceiling is an analytics parameter, and an order outside it is
        # recorded as nobody's rather than as the nearest session's.
        self._session(0, active_minutes=10)
        self._order(10 + 60 * (acr.PURCHASE_ATTRIBUTION_HOURS + 2))
        self.assertEqual(self._attributed(), {})

    def test_a_test_order_is_not_revenue(self):
        self._session(0)
        self._order(60, is_test=1)
        self.assertEqual(self._attributed(), {})

    # ── paid only ──

    def test_an_order_the_gateway_never_confirmed_is_not_revenue(self):
        # Created, pending, abandoned at the gateway: an orders row exists, no
        # payment_collector row does. Nobody assisted a sale that did not happen.
        self._session(0)
        self._order(60, payment=None)
        self.assertEqual(self._attributed(), {})

    def test_a_failed_payment_is_not_revenue(self):
        self._session(0)
        self._order(60, payment='failed')
        self.assertEqual(self._attributed(), {})

    def test_a_later_successful_attempt_after_a_failed_one_is_revenue(self):
        sid = self._session(0)
        order = self._order(60, payment='failed')
        self.cur.execute(
            "INSERT INTO payment_collector (order_id, payment_dump, status) "
            "VALUES (%s, '{}', 'TXN_SUCCESS')", (order,))
        self.assertEqual(self._attributed(), {sid: order})

    def test_an_unpaid_order_does_not_block_the_paid_one_behind_it(self):
        # The nearest rule picks the earliest eligible order; an unpaid one in
        # front of it is simply not eligible, so the paid one is credited.
        sid = self._session(0, active_minutes=600)
        self._order(60, payment=None)
        paid = self._order(120)
        self.assertEqual(self._attributed(), {sid: paid})

    def test_a_success_page_of_an_unpaid_order_proves_nothing(self):
        # The route records PURCHASE only when paid, but the ledger does not
        # trust the route: a stage row for an unpaid order attributes nothing.
        sid = self._session(0, customer=GUEST)
        order = self._order(30, payment=None)
        self._success_page(sid, order, 31)
        self.assertEqual(self._attributed(), {})

    # ── the same-browser proof ──

    def _success_page(self, sid, order_id, minutes):
        """The browser bound to ``sid`` rendered ``order_id``'s success page."""
        eid = acr.log_journey_stage(self.db, sid, acr.STAGE_PURCHASE,
                                    page_url="/success/%s" % order_id,
                                    order_id=order_id)
        self.assertTrue(eid)
        self.cur.execute("UPDATE ai_events SET created_at=%s WHERE event_id=%s",
                         (BASE + timedelta(minutes=minutes), eid))

    def _basis(self, sid):
        self.cur.execute("SELECT attribution_type, attribution_delta_seconds "
                         "FROM ai_session_commerce WHERE session_id=%s", (sid,))
        return self.cur.fetchone()

    def test_the_browser_that_saw_the_success_page_outranks_the_clock(self):
        # Two conversations; the *earlier* one's browser rendered the success
        # page, so it is the proven session, not the nearest one.
        proven = self._session(0)
        nearest = self._session(120)
        order = self._order(150)
        self._success_page(proven, order, 151)
        got = self._attributed()
        self.assertEqual(got[proven], order)
        self.assertNotIn(nearest, got)  # one order, one session
        row = self._basis(proven)
        self.assertEqual(row["attribution_type"], acr.ATTRIBUTION_SAME_BROWSER)
        self.assertEqual(row["attribution_delta_seconds"], 150 * 60)

    def test_a_guest_whose_browser_paid_is_attributed(self):
        guest = self._session(0, customer=GUEST)
        order = self._order(60)
        self._success_page(guest, order, 61)
        self.assertEqual(self._attributed(), {guest: order})
        self.assertEqual(self._basis(guest)["attribution_type"],
                         acr.ATTRIBUTION_SAME_BROWSER)

    def test_a_success_page_of_a_test_order_proves_nothing(self):
        sid = self._session(0)
        order = self._order(60, is_test=1)
        self._success_page(sid, order, 61)
        self.assertEqual(self._attributed(), {})

    def test_a_success_page_of_an_unknown_order_proves_nothing(self):
        sid = self._session(0, customer=GUEST)
        self._success_page(sid, "ORD_%s_nowhere" % self.tag, 61)
        self.assertEqual(self._attributed(), {})

    def test_the_time_rule_still_applies_without_a_success_page(self):
        sid = self._session(0)
        order = self._order(60)
        self.assertEqual(self._attributed(), {sid: order})
        self.assertEqual(self._basis(sid)["attribution_type"],
                         acr.ATTRIBUTION_NEAREST_PRECEDING)

    def test_two_sessions_in_the_same_second_resolve_to_one_winner(self):
        # Identical created_at: without the (created_at, session_id) tie-break
        # both would satisfy "no later session precedes the order" and the order
        # would be credited twice.
        a = self._session(60, session_id="attr_%s_tie_a" % self.tag)
        b = self._session(60, session_id="attr_%s_tie_b" % self.tag)
        order = self._order(90)
        got = self._attributed()
        self.assertEqual(len(got), 1, got)
        self.assertEqual(list(got.values()), [order])
        self.assertIn(list(got)[0], (a, b))

    def test_the_recorded_basis_can_be_re_derived(self):
        session = self._session(0)
        order = self._order(30)
        self._attributed()
        self.cur.execute(
            """SELECT order_id, attribution_type, attribution_window_hours,
                      attribution_delta_seconds
               FROM ai_session_commerce WHERE session_id=%s""", (session,))
        row = self.cur.fetchone()
        self.assertEqual(row["order_id"], order)
        self.assertEqual(row["attribution_type"],
                         acr.ATTRIBUTION_NEAREST_PRECEDING)
        self.assertEqual(row["attribution_window_hours"],
                         acr.PURCHASE_ATTRIBUTION_HOURS)
        # 30 minutes between the session start and the order.
        self.assertEqual(row["attribution_delta_seconds"], 1800)

    def test_a_second_sweep_credits_nothing_further(self):
        session = self._session(0)
        order = self._order(30)
        self.assertEqual(self._attributed(), {session: order})
        again = acr.attribute_archived_session_commerce(self.db, dry_run=False)
        self.assertEqual(again["attributed"], [])
        self.cur.execute(
            """SELECT COUNT(*) AS n FROM ai_events
               WHERE session_id=%s AND event_type=%s""",
            (session, acr.EV_COMMERCE_OUTCOME))
        self.assertEqual(self.cur.fetchone()["n"], 1)

    def test_the_unique_key_refuses_a_second_credit_for_one_order(self):
        # The backstop behind the query: even if a future rule change let two
        # sessions select one order, the ledger cannot hold both.
        first = self._session(0)
        order = self._order(30)
        self.assertEqual(self._attributed(), {first: order})
        second = self._session(10)
        with self.assertRaises(Exception):
            self.cur.execute(
                """INSERT INTO ai_session_commerce
                     (session_id, order_id, attribution_type,
                      attribution_window_hours, created_at)
                   VALUES (%s,%s,%s,%s,NOW())""",
                (second, order, acr.ATTRIBUTION_NEAREST_PRECEDING,
                 acr.PURCHASE_ATTRIBUTION_HOURS))

    def test_a_dry_run_records_nothing(self):
        self._session(0)
        self._order(30)
        result = acr.attribute_archived_session_commerce(self.db, dry_run=True)
        self.assertEqual([c["session_id"] for c in result["candidates"]
                          if c["session_id"] in set(self.sessions)],
                         self.sessions)
        self.cur.execute(
            """SELECT COUNT(*) AS n FROM ai_session_commerce
               WHERE session_id LIKE %s""", ("attr_%s%%" % self.tag,))
        self.assertEqual(self.cur.fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
