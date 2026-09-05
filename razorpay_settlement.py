"""Settling an Optiwar order against Razorpay's verified state.

Razorpay's server-side payment state is authoritative. The customer's browser
returning to ``/success`` is a UX event, not a condition for an order to become
paid: a shopper who completes UPI in their bank app and closes the tab has
paid, and the order must become ``Processed`` without them.

Three paths reach the same place:

    browser callback   /razorpay/verify   signature over order|payment
    webhook            /razorpay/webhook  signature over the raw body
    reconcile job      razorpay_reconcile every few minutes, for what both missed

Each resolves the Optiwar order the payment settles, checks captured /
amount / currency against the local order, and calls ``apply_paid_order`` —
whose UNIQUE key on ``payment_collector.payment_ref`` is the only idempotency
gate. Whichever path arrives first wins; every later one is a suppressed
duplicate. There is no second implementation of "payment successful".

Free of Flask and of the razorpay SDK: the API calls the fallbacks need are
passed in as callables, so every branch is testable without a network.
"""
import json

try:
    from .paid_orders import (apply_paid_order, order_amount_minor, order_currency,
                              order_statuses, PAID_STATUS)
except ImportError:      # loaded by path in tests, without the package
    from paid_orders import (apply_paid_order, order_amount_minor, order_currency,
                             order_statuses, PAID_STATUS)

# Razorpay order ``notes`` we write at creation, so a payment entity — which
# carries the order's notes — names our order on its own. The receipt stays
# as it always was; this is the same identifier, not a second one.
NOTE_ORDER_KEY = 'optiwar_order_id'
NOTE_HOST_KEY = 'host'

# How an order reference was found. Counted in the daily report.
BY_NOTES = 'notes'
BY_PAYMENT_LINK = 'payment_link'
BY_ORDER_PAYLOAD = 'order_payload'
BY_ORDER_LOOKUP = 'order_lookup'   # payment.order_id -> GET order -> receipt
BY_BROWSER = 'browser'             # the customer's own signed return

# Outcomes of settle(). Exactly one per call.
APPLIED = 'applied'
DUPLICATE = 'duplicate'
NOT_CAPTURED = 'not_captured'
UNKNOWN_ORDER = 'unknown_order'
AMOUNT_MISMATCH = 'amount_mismatch'
CURRENCY_MISMATCH = 'currency_mismatch'

# Legacy reference key some older payments carry in notes.
_LEGACY_NOTE_KEY = 'order_id'


def order_notes(order_id, host):
    return {NOTE_ORDER_KEY: str(order_id), NOTE_HOST_KEY: (host or '')}


def _note_ref(notes):
    notes = notes or {}
    if not isinstance(notes, dict):      # Razorpay sends [] for "no notes"
        return ''
    return (notes.get(NOTE_ORDER_KEY) or notes.get(_LEGACY_NOTE_KEY) or '').strip()


def resolve_order_reference(event, fetch_order=None, logger=None):
    """(payment entity, Optiwar order id, how it was found) for a paid event.

    Direct correlation first — payment notes, payment-link reference, the
    order entity when the payload carries one. When none of those name an
    order, and the payment names a Razorpay order, that order is fetched
    server-to-server and its ``receipt`` is the reference: a payment made
    against an order created before notes were written has nothing else.

    Never matches by customer, amount or time.
    """
    payload = (event.get('payload') or {})
    payment = ((payload.get('payment') or {}).get('entity') or {})

    ref = _note_ref(payment.get('notes'))
    if ref:
        return payment, ref, BY_NOTES

    link = ((payload.get('payment_link') or {}).get('entity') or {})
    ref = (link.get('reference_id') or _note_ref(link.get('notes')) or '').strip()
    if ref:
        return payment, ref, BY_PAYMENT_LINK

    rzp_order = ((payload.get('order') or {}).get('entity') or {})
    ref = (rzp_order.get('receipt') or _note_ref(rzp_order.get('notes')) or '').strip()
    if ref:
        return payment, ref, BY_ORDER_PAYLOAD

    rzp_order_id = (payment.get('order_id') or '').strip()
    if rzp_order_id and fetch_order is not None:
        fetched = None
        try:
            fetched = fetch_order(rzp_order_id) or {}
        except Exception as exc:  # noqa: BLE001 - a failed lookup is an unmatched event, not a crash
            if logger:
                logger.error("ACTIVITY:RAZORPAY_ORDER_LOOKUP_FAILED rzp_order:%s payment:%s err:%s"
                             % (rzp_order_id, payment.get('id', ''), exc))
        if fetched:
            ref = (fetched.get('receipt') or _note_ref(fetched.get('notes')) or '').strip()
            if ref:
                if logger:
                    logger.info("ACTIVITY:RAZORPAY_ORDER_LOOKUP_RECOVERED rzp_order:%s payment:%s order:%s"
                                % (rzp_order_id, payment.get('id', ''), ref))
                return payment, ref, BY_ORDER_LOOKUP
    return payment, '', ''


def settle(db, order_id, payment, site, source, method='', event='', logger=None,
           extra_dump=None):
    """Apply one Razorpay payment to one Optiwar order, if the evidence agrees.

    ``payment`` is a Razorpay payment entity (from a webhook, an API fetch or a
    reconcile). Its ``status`` must be ``captured``; its ``amount`` must not be
    short of the order total and its ``currency`` must be the order's. A
    payment entity without ``amount`` (a signature-only browser callback whose
    API fetch failed) skips the amount check and says so in the log.

    Returns ``{'outcome': ..., 'paid': <apply_paid_order result or None>,
    'order_id': ..., 'payment_id': ...}``.
    """
    payment_id = (payment.get('id') or '').strip()
    out = {'outcome': None, 'paid': None, 'order_id': order_id,
           'payment_id': payment_id, 'method': method, 'source': source}

    def log(level, tag, detail=''):
        if logger:
            getattr(logger, level)(
                "ACTIVITY:%s order:%s payment:%s source:%s method:%s%s"
                % (tag, order_id, payment_id, source, method or '-', detail))

    if payment.get('status') != 'captured':
        out['outcome'] = NOT_CAPTURED
        log('info', 'RAZORPAY_NOT_CAPTURED', ' status:%s' % payment.get('status'))
        return out

    cursor = db.cursor()
    expected_minor = order_amount_minor(cursor, order_id)
    if not expected_minor:
        out['outcome'] = UNKNOWN_ORDER
        log('error', 'RAZORPAY_UNKNOWN_ORDER')
        return out

    currency = order_currency(cursor, order_id)
    paid_currency = payment.get('currency') or currency
    if paid_currency != currency:
        out['outcome'] = CURRENCY_MISMATCH
        log('error', 'RAZORPAY_CURRENCY_MISMATCH',
            ' paid:%s expected:%s' % (paid_currency, currency))
        return out

    if payment.get('amount') is None:
        log('warning', 'RAZORPAY_AMOUNT_UNVERIFIED', ' expected:%s' % expected_minor)
    else:
        paid_minor = int(payment.get('amount') or 0)
        if paid_minor < expected_minor:
            out['outcome'] = AMOUNT_MISMATCH
            log('error', 'RAZORPAY_AMOUNT_MISMATCH',
                ' paid:%s expected:%s' % (paid_minor, expected_minor))
            return out
        if paid_minor > expected_minor:
            log('warning', 'RAZORPAY_OVERPAID',
                ' paid:%s expected:%s' % (paid_minor, expected_minor))

    dump = {'gateway': 'razorpay', 'source': source, 'resolved_by': method,
            'event': event, 'razorpay_payment_id': payment_id,
            'razorpay_order_id': payment.get('order_id', ''), 'payment': payment}
    if extra_dump:
        dump.update(extra_dump)
    paid = apply_paid_order(db, order_id, payment_id, dump, currency=currency,
                            site=site, gateway='razorpay', source=source,
                            logger=logger)
    out['paid'] = paid
    if not paid['applied']:
        out['outcome'] = DUPLICATE
        log('info', 'RAZORPAY_DUPLICATE_SUPPRESSED')
        return out
    out['outcome'] = APPLIED
    out['currency'] = currency
    out['amount_minor'] = expected_minor
    log('info', 'RAZORPAY_SETTLED',
        ' fulfilled:%s refund_pending:%s' % (paid['fulfilled_count'], len(paid['refund_lines'])))
    log('info', 'PAYMENT_SUCCESS', ' gateway:razorpay')
    return out


def notify_paid_order(cursor, order_id, settled, host, notify_success,
                      notify_confirmed, logger=None):
    """The customer acknowledgement a successful checkout sends, for a
    settlement that may have happened with no browser present.

    Sent only for an APPLIED outcome — a duplicate must not re-notify — and
    never for a test order. Failures are logged, never raised: money has been
    recorded by then, and a mail error must not undo that."""
    if settled.get('outcome') != APPLIED:
        return False
    paid = settled['paid']
    if paid.get('is_test'):
        return False
    try:
        cursor.execute(
            "SELECT c.customer_name, c.customer_email, c.customer_phone, "
            "ca.delivery_email, ca.delivery_phone "
            "FROM customers c JOIN orders o ON o.customer_id=c.customer_id "
            "LEFT JOIN customers_address ca ON ca.address_id=o.address_id "
            "WHERE o.order_id=%s LIMIT 1", (order_id,))
        cust = cursor.fetchone()
        if not cust:
            return False
        to_email = cust.get('delivery_email') or cust['customer_email']
        to_phone = cust.get('delivery_phone') or cust.get('customer_phone') or ''
        total = settled['amount_minor'] / 100.0
        symbol = '\u20b9' if settled['currency'] == 'INR' else '\u20ac'
        notify_success(to_email, to_phone, order_id, total, symbol, host,
                       'razorpay', profile_email=cust['customer_email'])
        if paid['fulfilled_count'] > 0:
            notify_confirmed(to_email, to_phone, cust.get('customer_name') or 'Customer',
                             order_id, total, symbol, host,
                             profile_email=cust['customer_email'])
        return True
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.error("EWS:ERROR payment_success_razorpay source:%s order:%s %s"
                         % (settled.get('source'), order_id, exc))
        return False


# ---------------------------------------------------------------------------
# Reconciliation: recent orders still Pending locally, asked of Razorpay.
# ---------------------------------------------------------------------------

RECONCILIATION_EXCEPTION = 'PAYMENT_RECONCILIATION_EXCEPTION'


def pending_orders(cursor, min_age_minutes=10, max_age_hours=72):
    """Orders whose latest status is Pending, with no successful payment row,
    created between ``min_age_minutes`` and ``max_age_hours`` ago.

    The lower bound leaves a checkout in progress alone; the upper bound is
    well past Razorpay's 24h webhook retry window. Gateway is not a column on
    an order, so every Pending order is a candidate: one Razorpay has no order
    for is simply left Pending."""
    cursor.execute(
        "SELECT o.order_id, MIN(o.date_created) AS date_created, "
        "MIN(o.site_from) AS site_from "
        "FROM orders o "
        "WHERE o.date_created BETWEEN NOW() - INTERVAL %s HOUR "
        "                         AND NOW() - INTERVAL %s MINUTE "
        "  AND NOT EXISTS (SELECT 1 FROM payment_collector pc "
        "                   WHERE pc.order_id = o.order_id AND pc.status='TXN_SUCCESS') "
        "GROUP BY o.order_id ORDER BY date_created",
        (int(max_age_hours), int(min_age_minutes)))
    rows = cursor.fetchall() or []
    out = []
    for row in rows:
        statuses = order_statuses(cursor, row['order_id'])
        latest = statuses[-1] if statuses else 'Pending'
        if latest == 'Pending' and PAID_STATUS not in statuses:
            out.append(row)
    return out


def _captured(payments):
    return [p for p in (payments or []) if p.get('status') == 'captured']


def reconcile_order(db, order_row, fetch_orders_by_receipt, fetch_order_payments,
                    logger=None, grace_minutes=30, now_ts=None):
    """Ask Razorpay about one Pending order. Returns a dict with ``verdict``:

    ``unpaid``      Razorpay has no captured payment — left Pending.
    ``settled``     exactly one captured payment reconciled — applied.
    ``duplicate``   already applied by another path meanwhile.
    ``exception``   evidence conflicts (amount/currency, several captured
                    payments, unknown order) — NOT applied, logged as
                    PAYMENT_RECONCILIATION_EXCEPTION.
    ``over_grace``  is True when a captured payment was older than the grace
                    period when found: both webhook and browser failed it.
    """
    order_id = order_row['order_id']
    site = order_row.get('site_from') or 'optiwar.com'
    out = {'order_id': order_id, 'verdict': 'unpaid', 'over_grace': False,
           'payment_id': '', 'detail': ''}

    def exception(detail):
        out['verdict'] = 'exception'
        out['detail'] = detail
        if logger:
            logger.error("ACTIVITY:%s order:%s payment:%s %s"
                         % (RECONCILIATION_EXCEPTION, order_id, out['payment_id'], detail))
        return out

    try:
        rzp_orders = fetch_orders_by_receipt(order_id) or []
    except Exception as exc:  # noqa: BLE001
        return exception('razorpay order lookup failed: %s' % exc)

    captured = []
    for rzp_order in rzp_orders:
        if rzp_order.get('receipt') != order_id:
            continue
        try:
            payments = fetch_order_payments(rzp_order['id']) or []
        except Exception as exc:  # noqa: BLE001
            return exception('razorpay payments lookup failed for %s: %s'
                             % (rzp_order.get('id'), exc))
        for p in _captured(payments):
            p = dict(p)
            p.setdefault('order_id', rzp_order['id'])
            captured.append(p)

    if not captured:
        return out
    if len(captured) > 1:
        out['payment_id'] = ','.join(p.get('id', '') for p in captured)
        return exception('%d captured payments for one order' % len(captured))

    payment = captured[0]
    out['payment_id'] = payment.get('id', '')
    created = payment.get('created_at')
    if now_ts is not None and created:
        out['over_grace'] = (now_ts - int(created)) > grace_minutes * 60

    settled = settle(db, order_id, payment, site=site, source='razorpay-reconcile',
                     method=BY_ORDER_LOOKUP, event='reconcile', logger=logger)
    out['settled'] = settled
    if settled['outcome'] == APPLIED:
        out['verdict'] = 'settled'
        if logger:
            logger.info("ACTIVITY:RAZORPAY_RECONCILE_RECOVERED order:%s payment:%s over_grace:%s"
                        % (order_id, out['payment_id'], int(out['over_grace'])))
    elif settled['outcome'] == DUPLICATE:
        out['verdict'] = 'duplicate'
    else:
        return exception('captured at razorpay but %s' % settled['outcome'])
    return out


def reconcile_pending(db, fetch_orders_by_receipt, fetch_order_payments, logger=None,
                      grace_minutes=30, min_age_minutes=10, max_age_hours=72,
                      now_ts=None, on_settled=None):
    """Run reconcile_order over every candidate. Returns a summary dict the
    cron wrapper writes as JSON for the daily report and the alert check."""
    cursor = db.cursor()
    candidates = pending_orders(cursor, min_age_minutes, max_age_hours)
    summary = {'checked': len(candidates), 'unpaid': 0, 'settled': 0,
               'duplicate': 0, 'exception': 0, 'over_grace': 0,
               'settled_orders': [], 'exceptions': []}
    for row in candidates:
        res = reconcile_order(db, row, fetch_orders_by_receipt, fetch_order_payments,
                              logger=logger, grace_minutes=grace_minutes, now_ts=now_ts)
        summary[res['verdict']] += 1
        if res['over_grace']:
            summary['over_grace'] += 1
            if logger:
                logger.error("ACTIVITY:PAYMENT_INVARIANT_RED order:%s payment:%s "
                             "captured at razorpay, pending locally beyond %d min"
                             % (row['order_id'], res['payment_id'], grace_minutes))
        if res['verdict'] == 'settled':
            summary['settled_orders'].append(res['order_id'])
            if on_settled is not None:
                on_settled(row, res['settled'])
        elif res['verdict'] == 'exception':
            summary['exceptions'].append({'order_id': res['order_id'],
                                          'payment_id': res['payment_id'],
                                          'detail': res['detail']})
    return summary


def summary_json(summary, generated_at):
    return json.dumps(dict(summary, generated_at=generated_at), default=str, indent=1)
