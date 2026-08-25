"""Whether a Razorpay webhook can be trusted, and what it is about.

No database and no SDK: these are the checks that stand between a POST from
anyone who guessed the URL and an order being marked paid.
"""
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "razorpay_events", os.path.join(REPO, "razorpay_events.py"))
razorpay_events = importlib.util.module_from_spec(_spec)
sys.modules["razorpay_events"] = razorpay_events
_spec.loader.exec_module(razorpay_events)

SECRET = 'whsec_test_value'


def sign(body, secret=SECRET):
    return hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()


class SignatureTest(unittest.TestCase):

    def test_body_signed_with_the_secret_is_accepted(self):
        body = b'{"event":"payment_link.paid"}'
        self.assertTrue(razorpay_events.verify_webhook_signature(
            body, sign(body), SECRET))

    def test_a_single_altered_byte_is_rejected(self):
        body = b'{"amount":49900}'
        signature = sign(body)
        self.assertFalse(razorpay_events.verify_webhook_signature(
            b'{"amount":19900}', signature, SECRET))

    def test_signature_from_another_secret_is_rejected(self):
        body = b'{"event":"order.paid"}'
        self.assertFalse(razorpay_events.verify_webhook_signature(
            body, sign(body, 'someone_elses_secret'), SECRET))

    def test_missing_signature_is_rejected(self):
        body = b'{}'
        self.assertFalse(razorpay_events.verify_webhook_signature(body, '', SECRET))
        self.assertFalse(razorpay_events.verify_webhook_signature(body, None, SECRET))

    def test_unconfigured_secret_verifies_nothing(self):
        body = b'{}'
        self.assertFalse(razorpay_events.verify_webhook_signature(body, sign(body), ''))
        self.assertFalse(razorpay_events.verify_webhook_signature(body, sign(body), None))

    def test_text_body_is_hashed_as_utf8(self):
        body = '{"note":"Ruché"}'
        self.assertTrue(razorpay_events.verify_webhook_signature(
            body, sign(body.encode('utf-8')), SECRET))


class PaymentEntityTest(unittest.TestCase):

    def test_order_id_from_payment_notes(self):
        event = {'event': 'payment.captured', 'payload': {'payment': {'entity': {
            'id': 'pay_1', 'amount': 49900, 'currency': 'INR',
            'notes': {'order_id': 'ABCDEF-123456'}}}}}
        payment, order_id = razorpay_events.payment_entity(event)
        self.assertEqual('ABCDEF-123456', order_id)
        self.assertEqual('pay_1', payment['id'])

    def test_order_id_from_payment_link_reference(self):
        event = {'event': 'payment_link.paid', 'payload': {
            'payment': {'entity': {'id': 'pay_2', 'amount': 49900}},
            'payment_link': {'entity': {'reference_id': 'LINKED-000001'}}}}
        payment, order_id = razorpay_events.payment_entity(event)
        self.assertEqual('LINKED-000001', order_id)
        self.assertEqual('pay_2', payment['id'])

    def test_order_id_from_razorpay_order_receipt(self):
        event = {'event': 'order.paid', 'payload': {
            'payment': {'entity': {'id': 'pay_3'}},
            'order': {'entity': {'receipt': 'RECPT-777777'}}}}
        _payment, order_id = razorpay_events.payment_entity(event)
        self.assertEqual('RECPT-777777', order_id)

    def test_padding_around_the_reference_is_stripped(self):
        event = {'event': 'payment_link.paid', 'payload': {
            'payment_link': {'entity': {'reference_id': '  SPACED-000002 '}}}}
        _payment, order_id = razorpay_events.payment_entity(event)
        self.assertEqual('SPACED-000002', order_id)

    def test_an_event_with_no_reference_yields_nothing(self):
        _payment, order_id = razorpay_events.payment_entity(
            {'event': 'payment.captured', 'payload': {'payment': {'entity': {'id': 'pay_4'}}}})
        self.assertEqual('', order_id)

    def test_empty_payload_does_not_raise(self):
        payment, order_id = razorpay_events.payment_entity({})
        self.assertEqual({}, payment)
        self.assertEqual('', order_id)

    def test_only_capture_events_count_as_paid(self):
        for kind in ('payment_link.paid', 'order.paid', 'payment.captured'):
            self.assertIn(kind, razorpay_events.PAID_EVENTS)
        for kind in ('payment_link.created', 'payment_link.expired',
                     'payment.failed', 'refund.processed', 'settlement.processed'):
            self.assertNotIn(kind, razorpay_events.PAID_EVENTS)

    def test_a_real_shaped_delivery_verifies_and_resolves(self):
        event = {'event': 'payment_link.paid', 'payload': {
            'payment': {'entity': {'id': 'pay_LiveLike', 'amount': 49900,
                                   'currency': 'INR', 'status': 'captured'}},
            'payment_link': {'entity': {'id': 'plink_1', 'status': 'paid',
                                        'reference_id': 'YYWODZ-451297'}}}}
        body = json.dumps(event).encode('utf-8')
        self.assertTrue(razorpay_events.verify_webhook_signature(
            body, sign(body), SECRET))
        payment, order_id = razorpay_events.payment_entity(json.loads(body))
        self.assertEqual('YYWODZ-451297', order_id)
        self.assertEqual(49900, payment['amount'])
        self.assertEqual('captured', payment['status'])


if __name__ == '__main__':
    unittest.main()
