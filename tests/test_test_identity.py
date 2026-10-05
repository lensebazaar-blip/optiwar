"""A TEST customer is a flagged production account: only it may buy through
/test-checkout, its test order is told to nobody, and only it is shown its own
TEST orders by the assistant."""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import order_lookup  # noqa: E402
import rx_lookup  # noqa: E402
import test_identity  # noqa: E402


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


class _Cursor:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.executed = list(rows or []), error, []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.error:
            raise self.error

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return self.rows.pop(0) if self.rows else []


class IsTestCustomer(unittest.TestCase):

    def test_only_a_flagged_account_is_a_test_customer(self):
        self.assertTrue(test_identity.is_test_customer(_Cursor([{"is_test": 1}]), 7))
        self.assertFalse(test_identity.is_test_customer(_Cursor([{"is_test": 0}]), 7))
        self.assertTrue(test_identity.is_test_customer(_Cursor([(1,)]), 7))

    def test_a_guest_or_unknown_id_is_not_and_is_not_queried(self):
        cur = _Cursor()
        self.assertFalse(test_identity.is_test_customer(cur, None))
        self.assertEqual(cur.executed, [])
        self.assertFalse(test_identity.is_test_customer(_Cursor([]), 7))

    def test_before_the_migration_nobody_is(self):
        cur = _Cursor(error=Exception("Unknown column 'is_test'"))
        self.assertFalse(test_identity.is_test_customer(cur, 7))

    def test_the_column_is_additive(self):
        (table, cols), = test_identity.ADDED_COLUMNS
        self.assertEqual(table, "customers")
        self.assertEqual(cols, (("is_test", "TINYINT(1) NOT NULL DEFAULT 0"),))


class AssistantSeesOwnTestOrdersOnly(unittest.TestCase):

    ROW = {"order_id": "T-1", "is_test_order": 1, "order_status_name": "Processed",
           "date_created": None, "payment_date": None, "product_name": "Frame"}

    def test_a_test_order_is_nobodys_by_default(self):
        self.assertEqual(order_lookup.group([dict(self.ROW)])["orders"]
                         + order_lookup.group([dict(self.ROW)])["unpaid"], [])

    def test_a_test_customer_is_shown_their_own(self):
        m = order_lookup.group([dict(self.ROW)], include_test=True)
        self.assertEqual([o["order_id"] for o in m["orders"] + m["unpaid"]], ["T-1"])

    def test_rx_orders_exclude_test_unless_the_customer_is_test(self):
        cur = _Cursor()
        rx_lookup.read_model(cur, [], 4242, "optiwar.com")
        self.assertIn("o.is_test=0", cur.executed[0][0])
        cur = _Cursor()
        rx_lookup.read_model(cur, [], 4242, "optiwar.com", include_test=True)
        sql, params = cur.executed[0]
        self.assertNotIn("is_test", sql)
        self.assertIn("o.customer_id=%s", sql)
        self.assertEqual(params, (4242, "optiwar.com"))

    def test_the_gateway_asks_the_signed_in_account_not_the_widget(self):
        src = _read("chat_gateway.py")
        self.assertEqual(src.count(
            "include_test=test_identity.is_test_customer(db.cursor(), customer_id)"), 2)
        self.assertIn("customer_id = flask_session.get('user_id')", src)


class TestCheckout(unittest.TestCase):

    def setUp(self):
        src = _read("models.py")
        self.route = src[src.index("def test_checkout():"):]
        self.route = self.route[:self.route.index("\n@bp.route")]

    def test_an_ordinary_customer_cannot_buy_through_test_checkout(self):
        gate = ("    is_test_customer = test_identity.is_test_customer("
                "get_db().cursor(), session.get('user_id'))\n"
                "    if not (current_app.config.get('TEST_PAY_ENABLED') or is_test_customer):\n"
                "        abort(404)\n")
        self.assertIn(gate, self.route)
        self.assertLess(self.route.index(gate), self.route.index("db.begin()"))

    def test_a_test_customers_test_order_is_told_to_nobody(self):
        notices = self.route[self.route.index("if not is_test_customer:"):]
        self.assertLess(notices.index("if not is_test_customer:"),
                        notices.index("notify_payment_success("))
        self.assertIn("notify_order_confirmed(", notices[:notices.index("except Exception")])

    def test_the_button_shows_for_a_test_customer(self):
        self.assertIn("bool(session.get('is_test_customer'))", _read("__init__.py"))
        self.assertIn("session['is_test_customer'] = is_test_customer(cursor, user['customer_id'])",
                      _read("auth.py"))


if __name__ == "__main__":
    unittest.main()
