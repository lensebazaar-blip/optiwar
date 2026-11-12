"""Refunds, against a real database and a fake payment provider.

The interesting questions about a refund are all about repetition and about
lying: does a double-click refund twice, does a provider failure leave an order
claiming to be refunded, can EU Ops talk this API into refunding EUR out of an
INR payment. None of those can be answered by a fake cursor — the UNIQUE key on
the idempotency key is the protection, and only rows show it working.

The provider is faked rather than the database: money must not move in a test,
but everything the provider is asked and told is asserted.

Skipped, not failed, when no test database is reachable.

    scripts/setup_test_db.sh
    python3 -m unittest tests.test_refunds
"""
import importlib.util
import os
import sys
import unittest
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


paid_orders = _load("paid_orders")
refunds = _load("refunds")

_MAIN_DB = os.environ.get("OPTIWAR_TEST_MYSQL_DB", "optiwar2")

DB_CONF = dict(
    host=os.environ.get("OPTIWAR_TEST_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("OPTIWAR_TEST_MYSQL_PORT", "3306")),
    user=os.environ.get("OPTIWAR_TEST_MYSQL_USER", "oslb6"),
    password=os.environ.get("OPTIWAR_TEST_MYSQL_PASSWORD", "testpw"),
    database=os.environ.get("OPTIWAR_TEST_REFUND_DB", _MAIN_DB + "_refunds"),
)

DDL = [
    """CREATE TABLE IF NOT EXISTS orders (
        order_line_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_id           VARCHAR(64) NOT NULL,
        product_id         BIGINT NULL,
        order_quantity     INT NOT NULL DEFAULT 1,
        order_total        INT NOT NULL DEFAULT 0,
        fulfillment_status ENUM('pending','fulfilled','refund_pending') NOT NULL DEFAULT 'pending',
        is_test            TINYINT NOT NULL DEFAULT 0,
        site_from          VARCHAR(64) NULL,
        KEY idx_order (order_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS order_status (
        order_status_id   BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_status_name VARCHAR(32) NOT NULL,
        order_id          VARCHAR(64) NOT NULL,
        source            VARCHAR(32) NULL,
        manual_flag       TINYINT NOT NULL DEFAULT 0,
        note              VARCHAR(255) NULL,
        created_at        DATETIME NULL,
        KEY idx_order (order_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS order_history (
        order_history_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_history_content TEXT NULL,
        order_id              VARCHAR(64) NOT NULL,
        site_from             VARCHAR(64) NULL,
        order_history_date    DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_order (order_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS payment_collector (
        id           BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_id     VARCHAR(64) NOT NULL,
        payment_ref  VARCHAR(191) NULL,
        payment_dump LONGTEXT NOT NULL,
        status       VARCHAR(32) NULL,
        date_created DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_payment_ref (payment_ref),
        KEY idx_order (order_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    refunds.SCHEMA,
]


def _connect():
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError:
        return None
    try:
        return pymysql.connect(cursorclass=DictCursor, autocommit=False, **DB_CONF)
    except Exception:
        return None


class FakeProvider:
    """Razorpay's answers, without Razorpay. Records every call."""

    def __init__(self, amount=99900, currency='INR', status='captured',
                 amount_refunded=0, refund_state='processed', fail=None):
        self.payment_entity = {
            'amount': amount, 'currency': currency, 'status': status,
            'amount_refunded': amount_refunded, 'gateway': 'razorpay'}
        self.refund_state = refund_state
        self.fail = fail
        self.refund_calls = []
        self.status_calls = []

    def payment(self, payment_ref):
        return dict(self.payment_entity, id=payment_ref)

    def refund(self, payment_ref, amount_minor, idempotency_key, notes=None):
        self.refund_calls.append((payment_ref, amount_minor, idempotency_key))
        if self.fail:
            raise RuntimeError(self.fail)
        return {'id': 'rfnd_%s' % len(self.refund_calls), 'amount': amount_minor,
                'currency': self.payment_entity['currency'],
                'status': self.refund_state, 'payment_id': payment_ref,
                'notes': notes or {}}

    def refund_status(self, provider_refund_id):
        self.status_calls.append(provider_refund_id)
        return {'id': provider_refund_id, 'status': self.refund_state}


class LockFakeCursor:
    def __init__(self, lock_result=1, used=None, release_error=None):
        self.lock_result = lock_result
        self.used = used
        self.release_error = release_error
        self.result = None
        self.queries = []
        self.release_calls = 0

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if query.startswith("SELECT GET_LOCK("):
            self.result = {'acquired': self.lock_result}
        elif query.startswith("SELECT order_id FROM order_refunds"):
            self.result = self.used
        elif query.startswith("SELECT RELEASE_LOCK("):
            self.release_calls += 1
            if self.release_error:
                raise self.release_error
            self.result = {'released': 1}
        else:
            raise AssertionError("unexpected query: %s" % query)

    def fetchone(self):
        return self.result


class LockFakeDB:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rollback_calls = 0

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rollback_calls += 1


class UpiUtrLockTest(unittest.TestCase):
    def setUp(self):
        self.cursor = LockFakeCursor()
        self.db = LockFakeDB(self.cursor)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.find_by_key = self.patches.enter_context(
            patch.object(refunds, 'find_by_key'))
        self.preview = self.patches.enter_context(
            patch.object(refunds, 'preview'))
        self.claim = self.patches.enter_context(patch.object(refunds, '_claim'))
        self.finish = self.patches.enter_context(patch.object(refunds, '_finish'))
        self.record_outcome = self.patches.enter_context(
            patch.object(refunds, '_record_outcome'))
        self.find_by_key.side_effect = [
            None, {'refund_id': 17, 'provider_refund_id': 'UPI:123456789012'}]
        self.preview.return_value = {
            'payment_status': 'PAID',
            'payment_ref': 'pay_lock_test',
            'provider_state': 'captured',
            'currency': 'INR',
            'captured_minor': 99900,
            'already_refunded_minor': 0,
            'max_refundable_minor': 99900,
        }
        self.claim.return_value = 17

    def _record(self):
        return refunds.record_upi(
            self.db, 'LOCK-TEST-1', amount_minor=25000, currency='INR',
            reason_code='CUSTOMER_REFUND', comment='test',
            idempotency_key='LOCK-TEST-1/refund/1', requested_by='ops',
            service_identity='eu-ops', approved_message='recorded',
            provider=FakeProvider(), upi_vpa='customer@okaxis',
            utr='123456789012',
            paid_on=datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat(),
            payee_name='Customer Name')

    def test_unavailable_utr_lock_refuses_without_claiming_a_row(self):
        self.cursor.lock_result = 0

        with self.assertRaises(refunds.RefundRejected) as caught:
            self._record()

        self.assertEqual(caught.exception.code, 'upi_record_busy')
        self.assertEqual(caught.exception.http_status, 409)
        self.claim.assert_not_called()
        self.assertEqual(self.cursor.release_calls, 0)
        self.assertFalse(any(
            query.startswith("SELECT order_id FROM order_refunds")
            for query, _ in self.cursor.queries))

    def test_utr_lock_is_released_after_success(self):
        result = self._record()

        self.assertFalse(result['replayed'])
        self.claim.assert_called_once()
        self.assertEqual(
            self.claim.call_args.kwargs['provider_refund_id'],
            'UPI:123456789012')
        self.finish.assert_called_once()
        self.assertEqual(self.cursor.release_calls, 1)
        self.assertEqual(
            self.cursor.queries[0][1], ('optiwar_upi_utr:123456789012',))

    def test_utr_lock_is_released_when_used_utr_validation_fails(self):
        self.cursor.used = {'order_id': 'OTHER-ORDER'}
        self.cursor.release_error = RuntimeError('release unavailable')

        with self.assertRaises(refunds.RefundRejected) as caught:
            self._record()

        self.assertEqual(caught.exception.code, 'utr_already_used')
        self.claim.assert_not_called()
        self.assertEqual(self.cursor.release_calls, 1)


class ClaimInsertCursor:
    def __init__(self):
        self.lastrowid = 23
        self.query = None
        self.params = None

    def execute(self, query, params=()):
        self.query = query
        self.params = params


class ClaimInsertDB:
    def __init__(self):
        self.commit_calls = 0

    def commit(self):
        self.commit_calls += 1


class ClaimInsertTest(unittest.TestCase):
    def _claim(self, **kwargs):
        cursor = ClaimInsertCursor()
        db = ClaimInsertDB()
        refund_id = refunds._claim(
            db, cursor, 'CLAIM-TEST-1', 'pay_claim_test', 25000, 'INR',
            refunds.PARTIAL, 'CUSTOMER_REFUND', 'test',
            'CLAIM-TEST-1/refund/1', 'ops', 'eu-ops', 'recorded', **kwargs)
        self.assertEqual(refund_id, 23)
        self.assertEqual(db.commit_calls, 1)
        self.assertIn('provider_refund_id', cursor.query)
        return cursor

    def test_upi_claim_insert_stores_provider_refund_id(self):
        cursor = self._claim(provider_refund_id='UPI:123456789012')

        self.assertEqual(cursor.params[-1], 'UPI:123456789012')

    def test_gateway_claim_defaults_provider_refund_id_to_none(self):
        cursor = self._claim()

        self.assertIsNone(cursor.params[-1])


class UpiKeyReplayTest(unittest.TestCase):
    def setUp(self):
        self.find_by_key = patch.object(refunds, 'find_by_key')
        self.find_by_key_mock = self.find_by_key.start()
        self.addCleanup(self.find_by_key.stop)
        self.db = LockFakeDB(LockFakeCursor())

    def _record(self, existing):
        self.find_by_key_mock.return_value = existing
        return refunds.record_upi(
            self.db, 'KEY-TEST-1', amount_minor=25000, currency='INR',
            reason_code='CUSTOMER_REFUND', comment='test',
            idempotency_key='KEY-TEST-1/refund/1', requested_by='ops',
            service_identity='eu-ops', approved_message='recorded',
            provider=FakeProvider(), upi_vpa='customer@okaxis',
            utr='123456789012', paid_on='2026-01-01',
            payee_name='Customer Name')

    def test_created_upi_row_is_an_inflight_replay(self):
        existing = {
            'refund_id': 17, 'status': refunds.CREATED,
            'provider_refund_id': 'UPI:123456789012',
        }

        result = self._record(existing)

        self.assertEqual(result['refund_id'], 17)
        self.assertTrue(result['replayed'])

    def test_created_gateway_row_without_provider_ref_is_not_replayed(self):
        existing = {
            'refund_id': 18, 'status': refunds.CREATED,
            'provider_refund_id': None,
        }

        with self.assertRaises(refunds.RefundRejected) as caught:
            self._record(existing)

        self.assertEqual(caught.exception.code, 'idempotency_key_reused')
        self.assertEqual(caught.exception.http_status, 409)

    def test_failed_row_is_not_replayed(self):
        existing = {
            'refund_id': 19, 'status': refunds.FAILED,
            'provider_refund_id': 'UPI:123456789012',
        }

        with self.assertRaises(refunds.RefundRejected) as caught:
            self._record(existing)

        self.assertEqual(caught.exception.code, 'idempotency_key_reused')
        self.assertEqual(caught.exception.http_status, 409)

    def test_gateway_refund_reference_is_not_replayed(self):
        existing = {
            'refund_id': 20, 'status': refunds.PROCESSED,
            'provider_refund_id': 'rfnd_existing',
        }

        with self.assertRaises(refunds.RefundRejected) as caught:
            self._record(existing)

        self.assertEqual(caught.exception.code, 'idempotency_key_reused')
        self.assertEqual(caught.exception.http_status, 409)


class RefundTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = _connect()
        if cls.db is None:
            raise unittest.SkipTest("no test database reachable")
        cur = cls.db.cursor()
        for stmt in DDL:
            cur.execute(stmt)
        cls.db.commit()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "db", None) is not None:
            cls.db.close()

    def setUp(self):
        self.cur = self.db.cursor()
        self._orders = []

    def tearDown(self):
        for order_id in self._orders:
            for table in ("order_refunds", "payment_collector", "order_history",
                          "order_status", "orders"):
                self.cur.execute("DELETE FROM %s WHERE order_id=%%s" % table,
                                 (order_id,))
        self.db.commit()

    # ─── fixtures ────────────────────────────────────────────────────

    def _order(self, total=999, paid=True, status='Processed',
               site='in.optiwar.com'):
        order_id = uuid.uuid4().hex[:6].upper() + "-RFND"
        self._orders.append(order_id)
        self.cur.execute(
            "INSERT INTO orders (order_id, product_id, order_quantity, "
            "order_total, fulfillment_status, is_test, site_from) "
            "VALUES (%s, 786, 1, %s, 'fulfilled', 0, %s)",
            (order_id, total, site))
        self.cur.execute(
            "INSERT INTO order_status (order_status_name, order_id) VALUES (%s,%s)",
            (status, order_id))
        if paid:
            self.cur.execute(
                "INSERT INTO payment_collector (order_id, payment_ref, "
                "payment_dump, status) VALUES (%s, %s, '{}', 'TXN_SUCCESS')",
                (order_id, 'pay_%s' % uuid.uuid4().hex[:12]))
        self.db.commit()
        return order_id

    def _key(self, order_id):
        return '%s/refund/%s' % (order_id, uuid.uuid4())

    def _refund(self, order_id, provider, amount=99900, currency='INR',
                reason='CUSTOMER_REFUND', key=None, requested_status=None):
        return refunds.execute(
            self.db, order_id, amount_minor=amount, currency=currency,
            reason_code=reason, comment='test', idempotency_key=key or self._key(order_id),
            requested_by='ops@lensbazaar', service_identity='eu-ops',
            approved_message='Your refund is on its way.', provider=provider,
            requested_status=requested_status)

    def _record_upi(self, order_id, provider, amount=99900, reason='CUSTOMER_REFUND',
                    key=None, **overrides):
        args = {
            'amount_minor': amount,
            'currency': 'INR',
            'reason_code': reason,
            'comment': 'test',
            'idempotency_key': key or self._key(order_id),
            'requested_by': 'ops@lensbazaar',
            'service_identity': 'eu-ops',
            'approved_message': 'Your UPI refund has been recorded.',
            'provider': provider,
            'upi_vpa': 'customer@okaxis',
            'utr': '123456789012',
            'paid_on': datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat(),
            'payee_name': 'Customer Name',
        }
        args.update(overrides)
        return refunds.record_upi(self.db, order_id, **args)

    def _statuses(self, order_id):
        return paid_orders.order_statuses(self.cur, order_id)

    def _history(self, order_id):
        self.cur.execute("SELECT order_history_content AS c, site_from FROM "
                         "order_history WHERE order_id=%s", (order_id,))
        return list(self.cur.fetchall())

    # ─── the preview ─────────────────────────────────────────────────

    def test_preview_reads_money_from_the_provider_not_the_order_total(self):
        # order_total says 999; the gateway captured 1149. What may be refunded
        # is what was actually taken.
        order_id = self._order(total=999)
        facts = refunds.preview(self.cur, order_id, FakeProvider(amount=114900))
        self.assertEqual(facts['payment_status'], 'PAID')
        self.assertEqual(facts['currency'], 'INR')
        self.assertEqual(facts['captured_minor'], 114900)
        self.assertEqual(facts['max_refundable_minor'], 114900)
        self.assertEqual(facts['storefront'], 'in.optiwar.com')

    def test_preview_counts_refunds_the_provider_knows_about(self):
        order_id = self._order()
        facts = refunds.preview(
            self.cur, order_id, FakeProvider(amount_refunded=50000))
        self.assertEqual(facts['already_refunded_minor'], 50000)
        self.assertEqual(facts['max_refundable_minor'], 49900)

    def test_preview_adds_upi_refunds_to_provider_reported_refunds(self):
        order_id = self._order()
        provider = FakeProvider(amount=139900, amount_refunded=50000)
        self._record_upi(order_id, provider, amount=30000)

        facts = refunds.preview(self.cur, order_id, provider)
        self.assertEqual(facts['already_refunded_minor'], 80000)
        self.assertEqual(facts['max_refundable_minor'], 59900)

    def test_preview_does_not_add_gateway_refunds_to_provider_tally_twice(self):
        order_id = self._order()
        provider = FakeProvider(amount=139900, amount_refunded=50000)
        self._refund(order_id, provider, amount=30000)

        facts = refunds.preview(self.cur, order_id, provider)
        self.assertEqual(facts['already_refunded_minor'], 50000)
        self.assertEqual(facts['max_refundable_minor'], 89900)

    def test_preview_of_an_unpaid_order_offers_nothing(self):
        order_id = self._order(paid=False, status='Pending')
        facts = refunds.preview(self.cur, order_id, FakeProvider())
        self.assertEqual(facts['payment_status'], 'UNPAID')
        self.assertEqual(facts['max_refundable_minor'], 0)
        self.assertIsNone(facts['currency'])

    def test_preview_of_a_missing_order_is_a_404(self):
        with self.assertRaises(refunds.RefundRejected) as caught:
            refunds.preview(self.cur, 'NO-SUCH-ORDER', FakeProvider())
        self.assertEqual(caught.exception.http_status, 404)

    # ─── the checks ──────────────────────────────────────────────────

    def _rejection(self, order_id, provider=None, **kw):
        with self.assertRaises(refunds.RefundRejected) as caught:
            self._refund(order_id, provider or FakeProvider(), **kw)
        return caught.exception.code

    def test_eur_against_an_inr_payment_is_refused_not_converted(self):
        order_id = self._order()
        self.assertEqual(self._rejection(order_id, currency='EUR'),
                         'currency_mismatch')
        self.assertEqual(self._statuses(order_id), ['Processed'])

    def test_more_than_was_captured_is_refused(self):
        order_id = self._order()
        self.assertEqual(self._rejection(order_id, amount=100000),
                         'amount_exceeds_refundable')

    def test_zero_and_negative_and_float_amounts_are_refused(self):
        order_id = self._order()
        self.assertEqual(self._rejection(order_id, amount=0),
                         'amount_not_positive')
        self.assertEqual(self._rejection(order_id, amount=-100),
                         'amount_not_positive')
        self.assertEqual(self._rejection(order_id, amount=999.0),
                         'amount_not_integer')

    def test_an_unpaid_order_cannot_be_refunded_or_marked_refunded(self):
        order_id = self._order(paid=False, status='Pending')
        self.assertEqual(
            self._rejection(order_id, requested_status='Refunded'),
            'order_not_paid')
        self.assertEqual(self._statuses(order_id), ['Pending'])

    def test_a_payment_the_provider_has_not_captured_is_refused(self):
        order_id = self._order()
        self.assertEqual(
            self._rejection(order_id, provider=FakeProvider(status='authorized')),
            'payment_not_captured')

    def test_an_unknown_reason_code_is_refused(self):
        order_id = self._order()
        self.assertEqual(self._rejection(order_id, reason='BECAUSE'),
                         'bad_reason_code')

    def test_a_partial_refund_cannot_declare_the_order_refunded(self):
        order_id = self._order()
        self.assertEqual(
            self._rejection(order_id, amount=50000, requested_status='Refunded'),
            'partial_cannot_be_refunded_status')

    # ─── execution ───────────────────────────────────────────────────

    def test_a_full_refund_records_ledger_history_and_status(self):
        order_id = self._order()
        provider = FakeProvider()
        row = self._refund(order_id, provider)

        self.assertEqual(row['status'], refunds.PROCESSED)
        self.assertEqual(row['refund_type'], refunds.FULL)
        self.assertEqual(int(row['amount_minor']), 99900)
        self.assertEqual(row['currency'], 'INR')
        self.assertEqual(row['provider_refund_id'], 'rfnd_1')
        self.assertIsNotNone(row['completed_at'])
        self.assertEqual(row['requested_by'], 'ops@lensbazaar')
        self.assertEqual(row['service_identity'], 'eu-ops')
        self.assertIn('Your refund is on its way.', row['approved_message'])

        self.assertEqual(len(provider.refund_calls), 1)
        self.assertEqual(provider.refund_calls[0][1], 99900)
        self.assertEqual(self._statuses(order_id), ['Processed', 'Refunded'])
        history = self._history(order_id)
        self.assertEqual(len(history), 1)
        self.assertIn('999.00', history[0]['c'])
        self.assertIn('rfnd_1', history[0]['c'])
        self.assertEqual(history[0]['site_from'], 'in.optiwar.com')

    def test_the_same_idempotency_key_refunds_once(self):
        order_id = self._order()
        provider = FakeProvider()
        key = self._key(order_id)
        first = self._refund(order_id, provider, key=key)
        second = self._refund(order_id, provider, key=key)

        self.assertEqual(len(provider.refund_calls), 1)
        self.assertEqual(first['refund_id'], second['refund_id'])
        self.assertTrue(second['replayed'])
        self.assertEqual(self._statuses(order_id).count('Refunded'), 1)
        self.cur.execute(
            "SELECT COUNT(*) AS n FROM order_refunds WHERE order_id=%s", (order_id,))
        self.assertEqual(self.cur.fetchone()['n'], 1)

    def test_a_second_refund_cannot_exceed_what_the_first_left(self):
        order_id = self._order()
        provider = FakeProvider()
        self._refund(order_id, provider, amount=40000)
        # The provider's own amount_refunded has not caught up; the ledger has.
        self.assertEqual(self._rejection(order_id, provider=provider,
                                         amount=60000),
                         'amount_exceeds_refundable')
        self.assertEqual(len(provider.refund_calls), 1)

    def test_two_partials_add_up_to_a_full_refund(self):
        order_id = self._order()
        provider = FakeProvider()
        first = self._refund(order_id, provider, amount=40000)
        second = self._refund(order_id, provider, amount=59900)

        self.assertEqual(first['refund_type'], refunds.PARTIAL)
        self.assertEqual(second['refund_type'], refunds.FULL)
        self.assertEqual(self._statuses(order_id),
                         ['Processed', 'Partially Refunded', 'Refunded'])
        self.assertEqual(len(self._history(order_id)), 2)

    def test_upi_full_refund_records_history_and_status_without_calling_provider(self):
        order_id = self._order()
        provider = FakeProvider()
        row = self._record_upi(order_id, provider)

        self.assertEqual(row['status'], refunds.PROCESSED)
        self.assertEqual(row['refund_type'], refunds.FULL)
        self.assertEqual(row['provider_refund_id'], 'UPI:123456789012')
        self.assertEqual(self._statuses(order_id), ['Processed', 'Refunded'])
        self.assertEqual(provider.refund_calls, [])
        history = self._history(order_id)
        self.assertEqual(len(history), 1)
        self.assertIn('paid by UPI to customer@okaxis (Customer Name)', history[0]['c'])
        self.assertIn('UTR 123456789012', history[0]['c'])

    def test_upi_partial_refund_records_partially_refunded_status(self):
        order_id = self._order()
        provider = FakeProvider()
        row = self._record_upi(order_id, provider, amount=25000)

        self.assertEqual(row['status'], refunds.PROCESSED)
        self.assertEqual(row['refund_type'], refunds.PARTIAL)
        self.assertEqual(self._statuses(order_id), ['Processed', 'Partially Refunded'])
        self.assertEqual(provider.refund_calls, [])

    def test_upi_idempotency_replay_does_not_create_a_second_row(self):
        order_id = self._order()
        provider = FakeProvider()
        key = self._key(order_id)
        first = self._record_upi(order_id, provider, key=key)
        second = self._record_upi(order_id, provider, key=key)

        self.assertFalse(first['replayed'])
        self.assertTrue(second['replayed'])
        self.assertEqual(first['refund_id'], second['refund_id'])
        self.assertEqual(provider.refund_calls, [])
        self.cur.execute(
            "SELECT COUNT(*) AS n FROM order_refunds WHERE order_id=%s", (order_id,))
        self.assertEqual(self.cur.fetchone()['n'], 1)

    def test_upi_rejects_reuse_of_a_failed_gateway_key(self):
        order_id = self._order()
        key = self._key(order_id)
        gateway = self._refund(
            order_id, FakeProvider(fail='gateway failed'), amount=25000, key=key)
        self.assertEqual(gateway['status'], refunds.FAILED)
        self.assertIsNone(gateway['provider_refund_id'])

        with self.assertRaises(refunds.RefundRejected) as caught:
            self._record_upi(order_id, FakeProvider(), amount=25000, key=key)

        self.assertEqual(caught.exception.code, 'idempotency_key_reused')
        self.assertEqual(caught.exception.http_status, 409)
        self.assertEqual(
            caught.exception.message,
            'this refund key belongs to a gateway refund attempt; start a new '
            'refund for the UPI payment')
        self.cur.execute(
            "SELECT COUNT(*) AS n FROM order_refunds WHERE order_id=%s", (order_id,))
        self.assertEqual(self.cur.fetchone()['n'], 1)

    def test_each_invalid_upi_field_is_rejected(self):
        order_id = self._order()
        provider = FakeProvider()
        today = datetime.now(ZoneInfo('Asia/Kolkata')).date()
        cases = [
            ({'upi_vpa': 'not-a-vpa'}, 'invalid_upi_id'),
            ({'utr': '12345'}, 'invalid_utr'),
            ({'paid_on': (today + timedelta(days=1)).isoformat()}, 'invalid_paid_on'),
            ({'paid_on': (today - timedelta(days=31)).isoformat()}, 'invalid_paid_on'),
            ({'paid_on': '2026-2-05'}, 'invalid_paid_on'),
            ({'payee_name': 'A'}, 'invalid_payee_name'),
            ({'payee_name': 'N' * 121}, 'invalid_payee_name'),
        ]
        for values, code in cases:
            with self.subTest(values=values):
                with self.assertRaises(refunds.RefundRejected) as caught:
                    self._record_upi(order_id, provider, **values)
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(provider.refund_calls, [])

    def test_upi_rejects_amount_above_max_refundable(self):
        order_id = self._order()
        with self.assertRaises(refunds.RefundRejected) as caught:
            self._record_upi(order_id, FakeProvider(), amount=99901)
        self.assertEqual(caught.exception.code, 'amount_exceeds_refundable')

    def test_upi_rejects_a_utr_already_used_on_another_order(self):
        first_order = self._order()
        second_order = self._order()
        self._record_upi(first_order, FakeProvider())

        with self.assertRaises(refunds.RefundRejected) as caught:
            self._record_upi(second_order, FakeProvider(), key=self._key(second_order))
        self.assertEqual(caught.exception.code, 'utr_already_used')
        self.assertIn(first_order, caught.exception.message)

    def test_created_upi_claim_counts_in_preview_and_blocks_utr_reuse(self):
        first_order = self._order()
        second_order = self._order()
        provider = FakeProvider()
        facts = refunds.preview(self.cur, first_order, provider)

        refunds._claim(
            self.db, self.cur, first_order, facts['payment_ref'], 25000, 'INR',
            refunds.PARTIAL, 'CUSTOMER_REFUND', 'test', self._key(first_order),
            'ops@lensbazaar', 'eu-ops', 'recorded',
            provider_refund_id='UPI:123456789012')

        facts = refunds.preview(self.cur, first_order, provider)
        self.assertEqual(facts['already_refunded_minor'], 25000)
        self.assertEqual(facts['max_refundable_minor'], 74900)
        with self.assertRaises(refunds.RefundRejected) as caught:
            self._record_upi(second_order, FakeProvider(),
                             key=self._key(second_order))
        self.assertEqual(caught.exception.code, 'utr_already_used')
        self.assertIn(first_order, caught.exception.message)

    def test_return_received_refund_cannot_use_upi(self):
        order_id = self._order()
        with self.assertRaises(refunds.RefundRejected) as caught:
            self._record_upi(order_id, FakeProvider(), reason='RETURN_RECEIVED')
        self.assertEqual(caught.exception.code, 'upi_not_for_return_refund')
        self.assertEqual(
            caught.exception.message,
            'return refunds run through the return assessment and the gateway')

    def test_gateway_preview_counts_a_recorded_upi_refund(self):
        order_id = self._order()
        provider = FakeProvider()
        self._record_upi(order_id, provider, amount=25000)

        facts = refunds.preview(self.cur, order_id, provider)
        self.assertEqual(facts['already_refunded_minor'], 25000)
        self.assertEqual(facts['max_refundable_minor'], 74900)

    def test_tracking_does_not_query_provider_for_a_processed_upi_refund(self):
        order_id = self._order()
        provider = FakeProvider()
        key = self._key(order_id)
        self._record_upi(order_id, provider, key=key)

        tracked = refunds.tracking(self.db, key, provider)
        self.assertEqual(tracked['status'], refunds.PROCESSED)
        self.assertEqual(provider.status_calls, [])

    def test_tracking_does_not_query_provider_for_a_created_upi_refund(self):
        order_id = self._order()
        provider = FakeProvider()
        facts = refunds.preview(self.cur, order_id, provider)
        key = self._key(order_id)
        refunds._claim(
            self.db, self.cur, order_id, facts['payment_ref'], 25000, 'INR',
            refunds.PARTIAL, 'CUSTOMER_REFUND', 'test', key,
            'ops@lensbazaar', 'eu-ops', 'recorded',
            provider_refund_id='UPI:123456789012')

        tracked = refunds.tracking(self.db, key, provider)

        self.assertEqual(tracked['status'], refunds.CREATED)
        self.assertEqual(provider.status_calls, [])

    def test_a_provider_failure_leaves_the_order_not_refunded(self):
        order_id = self._order()
        provider = FakeProvider(fail='refund not permitted')
        row = self._refund(order_id, provider)

        self.assertEqual(row['status'], refunds.FAILED)
        self.assertIn('refund not permitted', row['error_text'])
        self.assertIsNotNone(row['completed_at'])
        self.assertEqual(self._statuses(order_id), ['Processed'])
        self.assertEqual(self._history(order_id), [])

    def test_a_failed_attempt_releases_its_amount_for_a_retry(self):
        order_id = self._order()
        self._refund(order_id, FakeProvider(fail='timeout'))
        row = self._refund(order_id, FakeProvider())
        self.assertEqual(row['status'], refunds.PROCESSED)
        self.assertEqual(self._statuses(order_id), ['Processed', 'Refunded'])

    def test_a_pending_provider_refund_does_not_claim_the_money_landed(self):
        order_id = self._order()
        row = self._refund(order_id, FakeProvider(refund_state='pending'))
        self.assertEqual(row['status'], refunds.PENDING)
        self.assertIsNone(row['completed_at'])
        # Money left the merchant, so the order is refunded even while the
        # provider is still moving it — the ledger says PENDING, not the order.
        self.assertEqual(self._statuses(order_id), ['Processed', 'Refunded'])

    # ─── tracking ────────────────────────────────────────────────────

    def test_tracking_refreshes_a_pending_refund_from_the_provider(self):
        order_id = self._order()
        provider = FakeProvider(refund_state='pending')
        key = self._key(order_id)
        self._refund(order_id, provider, key=key)

        provider.refund_state = 'processed'
        live = refunds.tracking(self.db, key, provider)
        self.assertEqual(live['status'], refunds.PROCESSED)
        self.assertEqual(provider.status_calls, ['rfnd_1'])

        stored = refunds.find_by_key(self.db.cursor(), key)
        self.assertEqual(stored['status'], refunds.PROCESSED)
        self.assertIsNotNone(stored['completed_at'])

    def test_tracking_does_not_append_the_history_line_twice(self):
        order_id = self._order()
        provider = FakeProvider(refund_state='pending')
        key = self._key(order_id)
        self._refund(order_id, provider, key=key)
        provider.refund_state = 'processed'
        refunds.tracking(self.db, key, provider)
        refunds.tracking(self.db, key, provider)
        self.assertEqual(len(self._history(order_id)), 1)
        self.assertEqual(self._statuses(order_id), ['Processed', 'Refunded'])

    def test_tracking_says_so_when_an_accepted_refund_later_fails(self):
        order_id = self._order()
        provider = FakeProvider(refund_state='pending')
        key = self._key(order_id)
        self._refund(order_id, provider, key=key)
        provider.refund_state = 'failed'
        live = refunds.tracking(self.db, key, provider)
        self.assertEqual(live['status'], refunds.FAILED)
        contents = [h['c'] for h in self._history(order_id)]
        self.assertEqual(len(contents), 2)
        self.assertIn('reported FAILED', contents[1])

    def test_tracking_does_not_ask_the_provider_about_a_settled_refund(self):
        order_id = self._order()
        provider = FakeProvider()
        key = self._key(order_id)
        self._refund(order_id, provider, key=key)
        refunds.tracking(self.db, key, provider)
        self.assertEqual(provider.status_calls, [])

    def test_a_settled_refund_still_reports_the_providers_own_state(self):
        # The provider is not asked again, so the state it gave when it settled
        # is what the operator has to be shown next to the provider refund id.
        order_id = self._order()
        provider = FakeProvider()
        key = self._key(order_id)
        self._refund(order_id, provider, key=key)
        live = refunds.tracking(self.db, key, provider)
        self.assertEqual(live['status'], refunds.PROCESSED)
        self.assertEqual(live['provider_state'], 'processed')
        self.assertEqual(provider.status_calls, [])

    def test_tracking_an_unknown_key_is_a_404(self):
        with self.assertRaises(refunds.RefundRejected) as caught:
            refunds.tracking(self.db, 'nope/refund/1', FakeProvider())
        self.assertEqual(caught.exception.http_status, 404)

    # ─── money arithmetic ────────────────────────────────────────────

    def test_minor_units_render_without_floating_point(self):
        self.assertEqual(refunds.minor_to_major(139900), '1399.00')
        self.assertEqual(refunds.minor_to_major(99900), '999.00')
        self.assertEqual(refunds.minor_to_major(1), '0.01')
        self.assertEqual(refunds.minor_to_major(70), '0.70')


if __name__ == "__main__":
    unittest.main()
