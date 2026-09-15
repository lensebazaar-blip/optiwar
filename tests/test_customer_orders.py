"""The account panel shows the orders a customer paid for, once each.

Checkout writes an ``orders`` row before any money moves, so a customer who
retried a card three times had four PENDING cards in their history next to
the one order that exists. And ``order_status`` is append-only, so joining it
repeated every item once per status the order had been through.
"""
import datetime
import importlib.util
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name + "_under_test", os.path.join(REPO, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


co = _load("customer_orders")

WHEN = datetime.datetime(2026, 9, 8, 13, 6)


def line(order_id, status, paid, total=100, qty=1, product="Frame A"):
    return {
        "order_id": order_id, "order_status_name": status,
        "payment_date": WHEN if paid else None, "order_total": total,
        "order_quantity": qty, "product_name": product,
        "date_created": WHEN, "site_from": "optiwar.com",
    }


class CustomerOrdersTests(unittest.TestCase):

    def test_unpaid_attempts_are_not_the_customers_orders(self):
        """The screenshot: two Pending attempts above two real orders."""
        rows = [line("VQTVWY-021945", "Pending", False),
                line("GYBLCB-009832", "Pending", False),
                line("KJNPJB-286809", "Processed", True),
                line("FIADQU-063618", "Processed", True)]
        ids = [o["order_id"] for o in co.customer_orders(rows)]
        self.assertEqual(ids, ["KJNPJB-286809", "FIADQU-063618"])

    def test_failed_payment_is_hidden_too(self):
        self.assertEqual(
            co.customer_orders([line("X-1", "Payment Failed", False)]), [])

    def test_optiwar_has_no_cash_on_delivery(self):
        """Legacy COD status rows are just ops statuses; nothing says COD."""
        (order,) = co.customer_orders([line("X-2", "COD verified", False)])
        self.assertNotIn("pay_on_delivery", order)
        self.assertNotIn("delivery", order["stage_label"].lower())
        for name in co.STAGES.values():
            self.assertNotIn("pay on delivery", name[0].lower())
        with open(os.path.join(REPO, "templates", "profile.html")) as fh:
            self.assertNotIn("Pay on delivery", fh.read())
        with open(os.path.join(REPO, "paid_orders.py")) as fh:
            self.assertNotIn("COD", fh.read())

    def test_order_ops_moved_on_is_shown_without_a_payment_row(self):
        (order,) = co.customer_orders([line("X-3", "Shipped", False)])
        self.assertEqual(order["payment_state"], "paid")

    def test_lines_group_into_one_order_with_totals(self):
        rows = [line("X-4", "Processed", True, total=60, qty=1, product="A"),
                line("X-4", "Processed", True, total=80, qty=2, product="B")]
        (order,) = co.customer_orders(rows)
        self.assertEqual(len(order["items"]), 2)
        self.assertEqual(order["grand_total"], 140)
        self.assertEqual(order["item_count"], 3)

    def test_stage_maps_every_ops_status_to_a_customer_word(self):
        cases = {
            "Processed": ("Confirmed", 1), "Shipped": ("Shipped", 2),
            "Delivery-assist": ("Out for delivery", 2),
            "Complete": ("Delivered", 3), "Returned": ("Returned", 0),
            "Refunded": ("Refunded", 0),
            "Partially Refunded": ("Partially refunded", 0),
            "Shipped-Reverse": ("Return in progress", 0),
        }
        for status, (label, step) in cases.items():
            got = co.stage(status)
            self.assertEqual((got[0], got[2]), (label, step), status)

    def test_unknown_status_is_shown_as_itself(self):
        self.assertEqual(co.stage("Engraving")[0], "Engraving")

    def test_sql_reads_latest_status_and_payment_once_per_order(self):
        sql = co.ORDER_LINES_SQL
        self.assertIn("ORDER BY os.order_status_id DESC LIMIT 1", sql)
        self.assertIn("status='TXN_SUCCESS'", sql)
        self.assertIn("GROUP BY order_id", sql)
        self.assertNotIn("JOIN order_status os ON", sql)

    def test_profile_reads_orders_through_this_module(self):
        with open(os.path.join(REPO, "profile.py")) as fh:
            src = fh.read()
        self.assertIn("customer_orders(orders)", src)
        self.assertNotIn("JOIN order_status os", src)

    def test_template_uses_stage_not_raw_status(self):
        with open(os.path.join(REPO, "templates", "profile.html")) as fh:
            src = fh.read()
        self.assertIn("order.stage_label", src)
        self.assertIn("oh-track", src)
        self.assertNotIn("oh-status-pending", src)
        self.assertNotIn("order.payment_status", src)


if __name__ == "__main__":
    unittest.main()
