"""Reading a Razorpay webhook: signature first, then which order it is about.

Kept free of Flask and of the razorpay SDK so both halves can be tested for the
cases that matter — a tampered body, an event whose order id hides in a
different place depending on whether the customer paid a link, an order or a
checkout — without a running app.
"""
import hashlib
import hmac

# Events that mean money has actually been captured. Anything else (created,
# cancelled, expired, refunds, settlements) is not a paid order.
PAID_EVENTS = ('payment_link.paid', 'order.paid', 'payment.captured')


def verify_webhook_signature(raw_body, signature, secret):
    """HMAC-SHA256 of the raw body, as sent.

    The webhook signing secret is not the API key secret, and re-serialising the
    JSON before hashing breaks the comparison. No secret means no verification
    is possible, and an unverified webhook must never mark an order paid.
    """
    if not secret or not signature or raw_body is None:
        return False
    if isinstance(raw_body, str):
        raw_body = raw_body.encode('utf-8')
    expected = hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def payment_entity(event):
    """The payment entity of a paid event, and the Optiwar order id it settles.

    Razorpay puts our reference in a different field per flow: ``notes`` on a
    payment we created, ``reference_id`` on a payment link, ``receipt`` on an
    order. All three are checked so one handler serves every flow.
    """
    payload = (event.get('payload') or {})
    payment = ((payload.get('payment') or {}).get('entity') or {})
    order_id = (payment.get('notes') or {}).get('order_id', '')
    if not order_id:
        link = ((payload.get('payment_link') or {}).get('entity') or {})
        order_id = link.get('reference_id') or (link.get('notes') or {}).get('order_id', '')
    if not order_id:
        rzp_order = ((payload.get('order') or {}).get('entity') or {})
        order_id = rzp_order.get('receipt') or (rzp_order.get('notes') or {}).get('order_id', '')
    return payment, (order_id or '').strip()
