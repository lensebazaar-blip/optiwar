"""The order confirmation page must not call an unpaid order a success.

``/success/<order_id>`` renders for any order its owner asks for. An operator
creating an order for a payment link, or a checkout the gateway never came back
from, both leave a ``Pending`` order with no payment — and the page told the
customer "your order is a success", which is a lie they act on (and which hides
that we are waiting for money).

These tests pin the decision, including the two cases where "no payment row"
does not mean unpaid: cash on delivery, and an order ops has already moved on.
"""
import unittest

from paid_orders import order_payment_state, payment_state


class FakeCursor:
    """Answers the two queries ``order_payment_state`` asks, in order."""

    def __init__(self, has_payment, latest_status):
        self._has_payment = has_payment
        self._latest = latest_status
        self._result = None

    def execute(self, sql, params=()):
        if 'payment_collector' in sql:
            self._result = {'found': 1} if self._has_payment else None
        else:
            self._result = ({'order_status_name': self._latest}
                            if self._latest is not None else None)

    def fetchone(self):
        return self._result


class PaymentStateTests(unittest.TestCase):

    def test_successful_payment_is_paid(self):
        self.assertEqual(payment_state(True, 'Processed'), 'paid')

    def test_pending_without_payment_is_pending(self):
        """The ops-created order in the screenshot: Pending, no payment row."""
        self.assertEqual(payment_state(False, 'Pending'), 'pending')

    def test_no_status_at_all_is_pending(self):
        self.assertEqual(payment_state(False, ''), 'pending')
        self.assertEqual(payment_state(False, None), 'pending')

    def test_payment_failed_is_failed(self):
        self.assertEqual(payment_state(False, 'Payment Failed'), 'failed')

    def test_cod_is_confirmed_without_a_payment_row(self):
        for status in ('COD not verified', 'COD verfieid', 'COD verified'):
            self.assertEqual(payment_state(False, status), 'paid', status)

    def test_order_moved_on_by_ops_is_not_contradicted(self):
        """Money may have arrived outside the gateway; a shipped order is real."""
        for status in ('Processed', 'Shipped', 'Complete', 'Refunded'):
            self.assertEqual(payment_state(False, status), 'paid', status)

    def test_payment_row_wins_over_a_pending_status(self):
        """Webhook recorded the payment before the status row was appended."""
        self.assertEqual(payment_state(True, 'Pending'), 'paid')


class OrderPaymentStateTests(unittest.TestCase):

    def test_reads_payment_then_latest_status(self):
        state, latest = order_payment_state(FakeCursor(False, 'Pending'), 'X-1')
        self.assertEqual((state, latest), ('pending', 'Pending'))

    def test_paid_order(self):
        state, latest = order_payment_state(FakeCursor(True, 'Processed'), 'X-2')
        self.assertEqual((state, latest), ('paid', 'Processed'))

    def test_order_with_no_status_rows(self):
        state, latest = order_payment_state(FakeCursor(False, None), 'X-3')
        self.assertEqual((state, latest), ('pending', ''))


if __name__ == '__main__':
    unittest.main()
