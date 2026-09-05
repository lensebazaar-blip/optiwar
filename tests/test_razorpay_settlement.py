"""One captured Razorpay payment becomes exactly one paid Optiwar order.

The incident this guards against: a customer paid by UPI, never came back to
/success, and the ``payment.captured`` webhook could not name the order
because the payment carried no notes. Every recovery path — the browser, the
webhook with notes, the legacy webhook without notes resolved through
``payment.order_id -> Razorpay order -> receipt``, and the reconcile worker —
is exercised here against a real database, in every order they can arrive in,
and each combination must leave one payment row and one ``Processed`` status.

Razorpay itself is a dict of canned answers; the network is never touched.
Skipped when no test database is reachable (``scripts/setup_test_db.sh``).
"""
import importlib.util
import logging
import os
import sys
import time
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


paid_orders = _load("paid_orders")
rs = _load("razorpay_settlement")

from tests.test_paid_order_pipeline import DDL, _connect  # noqa: E402

# orders.date_created exists in production; the pipeline DDL predates the
# reconcile worker's need for it.
EXTRA_DDL = [
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS date_created DATETIME NULL "
    "DEFAULT CURRENT_TIMESTAMP",
    """CREATE TABLE IF NOT EXISTS customers (
        customer_id    BIGINT AUTO_INCREMENT PRIMARY KEY,
        customer_name  VARCHAR(100) NULL,
        customer_email VARCHAR(191) NULL,
        customer_phone VARCHAR(32) NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS customers_address (
        address_id     BIGINT AUTO_INCREMENT PRIMARY KEY,
        delivery_email VARCHAR(191) NULL,
        delivery_phone VARCHAR(32) NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

OUR_HOST = 'in.optiwar.com'


def rzp_payment(pid, rzp_order_id, amount, currency='INR', status='captured',
                notes=None, created_at=None):
    return {'id': pid, 'entity': 'payment', 'order_id': rzp_order_id,
            'amount': amount, 'currency': currency, 'status': status,
            'notes': notes if notes is not None else [],
            'created_at': created_at or int(time.time())}


def captured_event(payment, order_entity=None, kind='payment.captured'):
    payload = {'payment': {'entity': payment}}
    if order_entity is not None:
        payload['order'] = {'entity': order_entity}
    return {'event': kind, 'payload': payload}


class Razorpay:
    """Canned server-side answers, keyed by Razorpay order id."""

    def __init__(self):
        self.orders = {}
        self.payments = {}
        self.order_fetches = 0

    def add(self, rzp_order_id, receipt, payments=(), notes=None):
        self.orders[rzp_order_id] = {'id': rzp_order_id, 'receipt': receipt,
                                     'notes': notes if notes is not None else {}}
        self.payments[rzp_order_id] = list(payments)

    def fetch_order(self, rzp_order_id):
        self.order_fetches += 1
        return self.orders[rzp_order_id]

    def orders_by_receipt(self, receipt):
        return [o for o in self.orders.values() if o['receipt'] == receipt]

    def order_payments(self, rzp_order_id):
        return self.payments.get(rzp_order_id, [])


class Notifier:
    def __init__(self):
        self.success, self.confirmed = [], []

    def notify_success(self, *a, **kw):
        self.success.append((a, kw))

    def notify_confirmed(self, *a, **kw):
        self.confirmed.append((a, kw))


class SettlementDbTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = _connect()
        if cls.db is None:
            raise unittest.SkipTest("no test database reachable")
        cur = cls.db.cursor()
        for stmt in DDL + EXTRA_DDL:
            cur.execute(stmt)
        cls.db.commit()
        cls.log = logging.getLogger("test_razorpay_settlement")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "db", None) is not None:
            cls.db.close()

    def setUp(self):
        self.cur = self.db.cursor()
        self._orders, self._products, self._customers = [], [], []
        self.rzp = Razorpay()
        self.notes = Notifier()

    def tearDown(self):
        for order_id in self._orders:
            for table in ("sales_log", "payment_collector", "order_history",
                          "order_status", "orders"):
                self.cur.execute("DELETE FROM %s WHERE order_id=%%s" % table, (order_id,))
        for pid in self._products:
            self.cur.execute("DELETE FROM product_status_history WHERE product_id=%s", (pid,))
            self.cur.execute("DELETE FROM products WHERE product_id=%s", (pid,))
        for cid in self._customers:
            self.cur.execute("DELETE FROM customers WHERE customer_id=%s", (cid,))
        self.db.commit()

    # ─── fixtures ────────────────────────────────────────────────────

    def _order(self, total=949, qty=1, status='Pending', age_minutes=20, site=OUR_HOST,
               with_customer=True):
        self.cur.execute("INSERT INTO products (product_code, product_quantity, product_status) "
                         "VALUES (%s, 10, 'ACTIVE')", (uuid.uuid4().hex[:8].upper(),))
        pid = self.cur.lastrowid
        self._products.append(pid)
        cid = None
        if with_customer:
            self.cur.execute("INSERT INTO customers (customer_name, customer_email, customer_phone) "
                             "VALUES ('Test Customer', 'cust@example.test', '+910000000000')")
            cid = self.cur.lastrowid
            self._customers.append(cid)
        order_id = uuid.uuid4().hex[:6].upper() + "-RZP"
        self._orders.append(order_id)
        self.cur.execute(
            "INSERT INTO orders (order_id, product_id, order_quantity, order_total, "
            "fulfillment_status, is_test, site_from, customer_id, date_created) "
            "VALUES (%s, %s, %s, %s, 'pending', 0, %s, %s, NOW() - INTERVAL %s MINUTE)",
            (order_id, pid, qty, total, site, cid, int(age_minutes)))
        for name in ([status] if isinstance(status, str) else status):
            self.cur.execute("INSERT INTO order_status (order_status_name, order_id) "
                             "VALUES (%s, %s)", (name, order_id))
        self.db.commit()
        return order_id

    def _statuses(self, order_id):
        self.cur.execute("SELECT order_status_name, source FROM order_status WHERE order_id=%s "
                         "ORDER BY order_status_id", (order_id,))
        return [(r['order_status_name'], r['source']) for r in self.cur.fetchall()]

    def _payments(self, order_id=None, ref=None):
        if ref is not None:
            self.cur.execute("SELECT * FROM payment_collector WHERE payment_ref=%s", (ref,))
        else:
            self.cur.execute("SELECT * FROM payment_collector WHERE order_id=%s", (order_id,))
        return list(self.cur.fetchall())

    def _assert_paid_once(self, order_id, ref, source):
        st = self._statuses(order_id)
        self.assertEqual(1, [s for s, _ in st].count('Processed'), st)
        self.assertEqual(source, dict(st)['Processed'])
        rows = self._payments(order_id)
        self.assertEqual(1, len(rows))
        self.assertEqual(ref, rows[0]['payment_ref'])
        self.assertEqual('TXN_SUCCESS', rows[0]['status'])

    def _pay(self, order_id, amount=94900, notes=None, currency='INR', status='captured',
             created_at=None):
        rzp_order_id = 'order_' + uuid.uuid4().hex[:14]
        pid = 'pay_' + uuid.uuid4().hex[:14]
        payment = rzp_payment(pid, rzp_order_id, amount, currency, status, notes, created_at)
        self.rzp.add(rzp_order_id, order_id, [payment])
        return payment

    # the three arrival paths, as models.py / razorpay_reconcile.py drive them

    def _webhook(self, event):
        payment, order_id, method = rs.resolve_order_reference(
            event, fetch_order=self.rzp.fetch_order, logger=self.log)
        if not order_id:
            return {'outcome': 'unmatched', 'method': ''}
        settled = rs.settle(self.db, order_id, payment, site=OUR_HOST,
                            source='razorpay-webhook', method=method,
                            event=event['event'], logger=self.log)
        rs.notify_paid_order(self.db.cursor(), order_id, settled, OUR_HOST,
                             self.notes.notify_success, self.notes.notify_confirmed)
        return settled

    def _browser(self, order_id, payment):
        settled = rs.settle(self.db, order_id, payment, site=OUR_HOST, source='storefront',
                            method=rs.BY_BROWSER, event='browser_callback', logger=self.log)
        rs.notify_paid_order(self.db.cursor(), order_id, settled, OUR_HOST,
                             self.notes.notify_success, self.notes.notify_confirmed)
        return settled

    def _reconcile(self, now_ts=None, grace_minutes=30):
        settled = []
        summary = rs.reconcile_pending(
            self.db, self.rzp.orders_by_receipt, self.rzp.order_payments, logger=self.log,
            grace_minutes=grace_minutes, min_age_minutes=10, max_age_hours=72,
            now_ts=now_ts or int(time.time()),
            on_settled=lambda row, s: settled.append((row['order_id'], s)))
        summary['_settled'] = settled
        return summary

    # ─── normal paths ────────────────────────────────────────────────

    def test_normal_browser_return_pays_once_and_acknowledges(self):
        order_id = self._order()
        payment = self._pay(order_id)
        out = self._browser(order_id, payment)
        self.assertEqual(rs.APPLIED, out['outcome'])
        self._assert_paid_once(order_id, payment['id'], 'storefront')
        self.assertEqual(1, len(self.notes.success))
        self.assertEqual(1, len(self.notes.confirmed))

    def test_webhook_with_notes_pays_by_notes(self):
        order_id = self._order()
        payment = self._pay(order_id, notes=rs.order_notes(order_id, OUR_HOST))
        out = self._webhook(captured_event(payment))
        self.assertEqual(rs.APPLIED, out['outcome'])
        self.assertEqual(rs.BY_NOTES, out['method'])
        self.assertEqual(0, self.rzp.order_fetches, "notes were enough; no lookup")
        self._assert_paid_once(order_id, payment['id'], 'razorpay-webhook')

    def test_legacy_webhook_without_notes_recovers_through_order_receipt(self):
        """Customer pays in the UPI app, closes the tab: the incident."""
        order_id = self._order()
        payment = self._pay(order_id, notes=[])
        out = self._webhook(captured_event(payment))
        self.assertEqual(rs.APPLIED, out['outcome'])
        self.assertEqual(rs.BY_ORDER_LOOKUP, out['method'])
        self.assertEqual(1, self.rzp.order_fetches)
        self._assert_paid_once(order_id, payment['id'], 'razorpay-webhook')
        self.assertEqual(1, len(self.notes.success), "abandoned browser still gets the ack")
        self.assertEqual(1, len(self.notes.confirmed))
        rows = self._payments(ref=payment['id'])
        self.assertIn(payment['order_id'], rows[0]['payment_dump'])
        self.assertIn('"resolved_by": "order_lookup"', rows[0]['payment_dump'])

    def test_order_paid_event_uses_the_order_entity_receipt(self):
        order_id = self._order()
        payment = self._pay(order_id)
        event = captured_event(payment, order_entity=self.rzp.orders[payment['order_id']],
                               kind='order.paid')
        out = self._webhook(event)
        self.assertEqual(rs.APPLIED, out['outcome'])
        self.assertEqual(rs.BY_ORDER_PAYLOAD, out['method'])
        self._assert_paid_once(order_id, payment['id'], 'razorpay-webhook')

    # ─── ordering and duplicates ─────────────────────────────────────

    def test_webhook_then_browser_pays_once_notifies_once(self):
        order_id = self._order()
        payment = self._pay(order_id)
        self.assertEqual(rs.APPLIED, self._webhook(captured_event(payment))['outcome'])
        self.assertEqual(rs.DUPLICATE, self._browser(order_id, payment)['outcome'])
        self._assert_paid_once(order_id, payment['id'], 'razorpay-webhook')
        self.assertEqual(1, len(self.notes.success))

    def test_browser_then_webhook_pays_once_notifies_once(self):
        order_id = self._order()
        payment = self._pay(order_id)
        self.assertEqual(rs.APPLIED, self._browser(order_id, payment)['outcome'])
        self.assertEqual(rs.DUPLICATE, self._webhook(captured_event(payment))['outcome'])
        self._assert_paid_once(order_id, payment['id'], 'storefront')
        self.assertEqual(1, len(self.notes.success))

    def test_duplicate_payment_captured_is_suppressed(self):
        order_id = self._order()
        payment = self._pay(order_id)
        event = captured_event(payment)
        self.assertEqual(rs.APPLIED, self._webhook(event)['outcome'])
        self.assertEqual(rs.DUPLICATE, self._webhook(event)['outcome'])
        self.assertEqual(rs.DUPLICATE, self._webhook(event)['outcome'])
        self._assert_paid_once(order_id, payment['id'], 'razorpay-webhook')
        self.assertEqual(1, len(self.notes.success))

    def test_payment_captured_then_order_paid_for_the_same_payment_pays_once(self):
        order_id = self._order()
        payment = self._pay(order_id)
        self.assertEqual(rs.APPLIED, self._webhook(captured_event(payment))['outcome'])
        late = captured_event(payment, order_entity=self.rzp.orders[payment['order_id']],
                              kind='order.paid')
        self.assertEqual(rs.DUPLICATE, self._webhook(late)['outcome'])
        self._assert_paid_once(order_id, payment['id'], 'razorpay-webhook')

    def test_all_three_paths_then_reconcile_pays_once(self):
        order_id = self._order()
        payment = self._pay(order_id)
        self._browser(order_id, payment)
        self._webhook(captured_event(payment))
        summary = self._reconcile()
        self.assertEqual(0, summary['settled'])
        self._assert_paid_once(order_id, payment['id'], 'storefront')

    def test_one_payment_cannot_settle_two_orders(self):
        first = self._order()
        second = self._order()
        payment = self._pay(first)
        self.assertEqual(rs.APPLIED, self._browser(first, payment)['outcome'])
        # the same payment id presented for another order: the unique key refuses it
        out = self._browser(second, payment)
        self.assertEqual(rs.DUPLICATE, out['outcome'])
        self.assertEqual([('Pending', None)], self._statuses(second))
        self.assertEqual(1, len(self._payments(ref=payment['id'])))

    def test_already_processed_order_gets_no_second_processed_row(self):
        order_id = self._order(status=['Pending', 'Processed'])
        payment = self._pay(order_id)
        out = self._webhook(captured_event(payment))
        # a new payment id is a new payment row, but Processed is appended once
        self.assertEqual(rs.APPLIED, out['outcome'])
        self.assertFalse(out['paid']['status_appended'])
        self.assertEqual(1, [s for s, _ in self._statuses(order_id)].count('Processed'))

    # ─── refusals ────────────────────────────────────────────────────

    def test_wrong_amount_is_refused_and_leaves_pending(self):
        order_id = self._order(total=949)
        payment = self._pay(order_id, amount=94800)
        out = self._webhook(captured_event(payment))
        self.assertEqual(rs.AMOUNT_MISMATCH, out['outcome'])
        self.assertEqual([('Pending', None)], self._statuses(order_id))
        self.assertEqual([], self._payments(order_id))
        self.assertEqual([], self.notes.success)

    def test_wrong_currency_is_refused_and_leaves_pending(self):
        order_id = self._order()
        payment = self._pay(order_id, currency='EUR')
        out = self._webhook(captured_event(payment))
        self.assertEqual(rs.CURRENCY_MISMATCH, out['outcome'])
        self.assertEqual([('Pending', None)], self._statuses(order_id))
        self.assertEqual([], self._payments(order_id))

    def test_unknown_receipt_is_refused(self):
        payment = rzp_payment('pay_unknown1', 'order_unknown1', 94900)
        self.rzp.add('order_unknown1', 'NOSUCH-ORDER', [payment])
        out = self._webhook(captured_event(payment))
        self.assertEqual(rs.UNKNOWN_ORDER, out['outcome'])
        self.assertEqual([], self._payments(ref='pay_unknown1'))

    def test_uncaptured_payment_does_nothing(self):
        order_id = self._order()
        payment = self._pay(order_id, status='authorized')
        out = self._webhook(captured_event(payment))
        self.assertEqual(rs.NOT_CAPTURED, out['outcome'])
        self.assertEqual([('Pending', None)], self._statuses(order_id))

    def test_payment_naming_no_order_anywhere_is_unmatched_not_guessed(self):
        order_id = self._order()
        payment = rzp_payment('pay_orphan1', '', 94900)      # no order_id, no notes
        out = self._webhook(captured_event(payment))
        self.assertEqual('unmatched', out['outcome'])
        self.assertEqual([('Pending', None)], self._statuses(order_id))

    # ─── the reconcile worker ────────────────────────────────────────

    def test_reconcile_applies_a_captured_payment_nobody_reported(self):
        order_id = self._order(age_minutes=45)
        payment = self._pay(order_id, created_at=int(time.time()) - 45 * 60)
        summary = self._reconcile()
        self.assertEqual(1, summary['checked'])
        self.assertEqual(1, summary['settled'])
        self.assertEqual(1, summary['over_grace'], "45 min > 30 min grace: RED")
        self.assertEqual([order_id], summary['settled_orders'])
        self._assert_paid_once(order_id, payment['id'], 'razorpay-reconcile')
        self.assertEqual(1, len(summary['_settled']))
        # rerun: nothing left to do, nothing applied twice
        again = self._reconcile()
        self.assertEqual(0, again['checked'])
        self._assert_paid_once(order_id, payment['id'], 'razorpay-reconcile')

    def test_reconcile_within_grace_settles_without_red(self):
        order_id = self._order(age_minutes=15)
        self._pay(order_id, created_at=int(time.time()) - 15 * 60)
        summary = self._reconcile()
        self.assertEqual(1, summary['settled'])
        self.assertEqual(0, summary['over_grace'])

    def test_reconcile_leaves_an_unpaid_order_pending(self):
        order_id = self._order(age_minutes=40)
        rzp_order_id = 'order_' + uuid.uuid4().hex[:14]
        self.rzp.add(rzp_order_id, order_id, [
            rzp_payment('pay_failed1', rzp_order_id, 94900, status='failed')])
        summary = self._reconcile()
        self.assertEqual(1, summary['checked'])
        self.assertEqual(1, summary['unpaid'])
        self.assertEqual(0, summary['settled'])
        self.assertEqual([('Pending', None)], self._statuses(order_id))

    def test_reconcile_ignores_orders_razorpay_never_heard_of(self):
        order_id = self._order(age_minutes=40)   # e.g. a COD or Paytm order
        summary = self._reconcile()
        self.assertEqual(1, summary['unpaid'])
        self.assertEqual([('Pending', None)], self._statuses(order_id))

    def test_reconcile_skips_fresh_orders_and_non_pending_ones(self):
        self._order(age_minutes=2)
        self._order(age_minutes=40, status=['Pending', 'Cancelled'])
        summary = self._reconcile()
        self.assertEqual(0, summary['checked'])

    def test_reconcile_raises_exception_on_conflicting_amount_not_applied(self):
        order_id = self._order(total=949, age_minutes=40)
        self._pay(order_id, amount=50000)
        summary = self._reconcile()
        self.assertEqual(1, summary['exception'])
        self.assertEqual(0, summary['settled'])
        self.assertIn('amount_mismatch', summary['exceptions'][0]['detail'])
        self.assertEqual([('Pending', None)], self._statuses(order_id))

    def test_reconcile_raises_exception_on_two_captured_payments(self):
        order_id = self._order(age_minutes=40)
        rzp_order_id = 'order_' + uuid.uuid4().hex[:14]
        self.rzp.add(rzp_order_id, order_id, [
            rzp_payment('pay_twice_a', rzp_order_id, 94900),
            rzp_payment('pay_twice_b', rzp_order_id, 94900)])
        summary = self._reconcile()
        self.assertEqual(1, summary['exception'])
        self.assertEqual([('Pending', None)], self._statuses(order_id))
        self.assertEqual([], self._payments(order_id))

    def test_reconcile_after_webhook_finds_nothing(self):
        order_id = self._order(age_minutes=40)
        payment = self._pay(order_id)
        self._webhook(captured_event(payment))
        summary = self._reconcile()
        self.assertEqual(0, summary['checked'])
        self._assert_paid_once(order_id, payment['id'], 'razorpay-webhook')

    def test_reconcile_treats_a_lookup_failure_as_exception_not_unpaid(self):
        self._order(age_minutes=40)

        def boom(receipt):
            raise RuntimeError("razorpay 5xx")
        summary = rs.reconcile_pending(self.db, boom, self.rzp.order_payments,
                                       logger=self.log, now_ts=int(time.time()))
        self.assertEqual(1, summary['exception'])


class ResolveReferenceTest(unittest.TestCase):
    """The reference chain without a database."""

    def test_notes_win_over_everything(self):
        p = rzp_payment('pay_1', 'order_1', 1, notes={'optiwar_order_id': 'A-1'})
        calls = []
        _, ref, how = rs.resolve_order_reference(
            captured_event(p, order_entity={'receipt': 'B-2'}), fetch_order=calls.append)
        self.assertEqual(('A-1', rs.BY_NOTES), (ref, how))
        self.assertEqual([], calls)

    def test_legacy_note_key_is_still_read(self):
        p = rzp_payment('pay_1', 'order_1', 1, notes={'order_id': 'A-1'})
        self.assertEqual('A-1', rs.resolve_order_reference(captured_event(p))[1])

    def test_payment_link_reference(self):
        p = rzp_payment('pay_1', '', 1)
        ev = captured_event(p, kind='payment_link.paid')
        ev['payload']['payment_link'] = {'entity': {'reference_id': 'L-9'}}
        self.assertEqual(('L-9', rs.BY_PAYMENT_LINK), rs.resolve_order_reference(ev)[1:])

    def test_order_lookup_is_the_last_resort_and_uses_receipt(self):
        p = rzp_payment('pay_1', 'order_X', 1)
        _, ref, how = rs.resolve_order_reference(
            captured_event(p), fetch_order=lambda oid: {'id': oid, 'receipt': 'R-7'})
        self.assertEqual(('R-7', rs.BY_ORDER_LOOKUP), (ref, how))

    def test_order_lookup_falls_back_to_order_notes(self):
        p = rzp_payment('pay_1', 'order_X', 1)
        _, ref, _ = rs.resolve_order_reference(
            captured_event(p), fetch_order=lambda oid: {'id': oid, 'receipt': '',
                                                        'notes': {'optiwar_order_id': 'N-3'}})
        self.assertEqual('N-3', ref)

    def test_failed_lookup_is_unmatched(self):
        p = rzp_payment('pay_1', 'order_X', 1)

        def boom(oid):
            raise RuntimeError("timeout")
        self.assertEqual('', rs.resolve_order_reference(captured_event(p), fetch_order=boom)[1])

    def test_no_fetcher_no_reference(self):
        p = rzp_payment('pay_1', 'order_X', 1)
        self.assertEqual('', rs.resolve_order_reference(captured_event(p))[1])

    def test_razorpay_empty_list_notes_do_not_crash(self):
        p = rzp_payment('pay_1', '', 1, notes=[])
        self.assertEqual('', rs.resolve_order_reference(captured_event(p))[1])

    def test_order_notes_carry_the_same_id_as_the_receipt(self):
        notes = rs.order_notes('BSNICP-523998', 'optiwar.in')
        self.assertEqual({'optiwar_order_id': 'BSNICP-523998', 'host': 'optiwar.in'}, notes)


if __name__ == "__main__":
    unittest.main()
