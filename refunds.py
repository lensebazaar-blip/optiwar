"""Refunding a paid order: the facts, the checks, and the ledger.

A refund is money leaving, so nothing here trusts the caller. The EU Ops
dashboard sends an instruction — how much, in what currency, why — and every
value it could get wrong is looked up again from Optiwar's own records and from
the payment provider:

    which storefront owns the order      orders.site_from, never the caller
    the currency                         the captured payment, never the caller
    what was captured                    the provider, in minor units
    what is already refunded             the provider plus this ledger
    whether a refund may happen at all   the checks in ``validate``

Deliberately free of Flask and of the provider SDK: the provider is a small
object passed in, so every rejection and every double-submit can be tested
against a real database without money or a network.

Amounts are integers in the currency's minor unit throughout. Binary floating
point does not appear in a refund path — ``order_total`` is read only to show
the operator a line breakdown, never to decide what to refund.
"""
import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from . import return_assessment
    from .paid_orders import add_history, append_status
except ImportError:      # loaded by path in tests, without the package
    import return_assessment
    from paid_orders import add_history, append_status

# What the ledger row means, and therefore what the operator is shown.
CREATED = 'CREATED'        # we are about to ask the provider
PENDING = 'PENDING'        # provider accepted, money not yet with the customer
PROCESSED = 'PROCESSED'    # provider says the refund is done
FAILED = 'FAILED'          # provider refused, or we never got an answer

# Provider refund states, mapped to ours.
_PROVIDER_STATE = {'processed': PROCESSED, 'pending': PENDING,
                   'failed': FAILED, 'created': PENDING}

FULL = 'FULL'
PARTIAL = 'PARTIAL'

# Status a refunded order may be moved to. A refund is not a status change, so
# the ledger is written either way; this is only what the customer's order says.
STATUS_FULL = 'Refunded'
STATUS_PARTIAL = 'Partially Refunded'

REASON_CODES = ('CUSTOMER_REFUND', 'OUT_OF_STOCK', 'DUPLICATE_PAYMENT',
                'ORDER_CANCELLED', 'GOODWILL', 'RETURN_RECEIVED')

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_refunds (
    refund_id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id           VARCHAR(255) NOT NULL,
    payment_ref        VARCHAR(191) NOT NULL,
    provider_refund_id VARCHAR(191) NULL,
    amount_minor       BIGINT NOT NULL,
    currency           VARCHAR(8) NOT NULL,
    status             VARCHAR(16) NOT NULL,
    refund_type        VARCHAR(8) NOT NULL,
    reason_code        VARCHAR(64) NOT NULL,
    comment            TEXT NULL,
    idempotency_key    VARCHAR(191) NOT NULL,
    requested_by       VARCHAR(191) NOT NULL,
    service_identity   VARCHAR(64) NOT NULL,
    approved_message   MEDIUMTEXT NULL,
    provider_response  MEDIUMTEXT NULL,
    error_text         VARCHAR(512) NULL,
    requested_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at       DATETIME NULL,
    UNIQUE KEY uk_idempotency (idempotency_key),
    KEY idx_order (order_id),
    KEY idx_payment (payment_ref)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def minor_to_major(amount_minor):
    """``139900`` -> ``'1399.00'``, without going near a float."""
    return '%d.%02d' % (amount_minor // 100, amount_minor % 100)


class RefundRejected(Exception):
    """A check failed. ``code`` is stable enough for the dashboard to branch on."""

    def __init__(self, code, message, http_status=422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


_SCHEMA_READY = False


def ensure_schema(cursor):
    """Create the ledger if it is absent. Idempotent, additive, no ALTERs.

    Once per process: ``CREATE TABLE`` commits implicitly, so running it inside
    a refund's transaction would break the very atomicity the ledger is for.
    Normally the table is already there, applied deliberately by
    ``deploy.py migrate`` before the code that uses it is deployed.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    cursor.execute(SCHEMA)
    cursor.execute(return_assessment.SCHEMA)
    _SCHEMA_READY = True


def _site(cursor, order_id):
    cursor.execute("SELECT MIN(site_from) AS site, MAX(is_test) AS is_test, "
                   "COUNT(*) AS line_count FROM orders WHERE order_id=%s",
                   (order_id,))
    return cursor.fetchone() or {}


def _successful_payment(cursor, order_id):
    """The captured payment for an order, or None.

    The newest successful row wins: a first attempt that failed leaves a row
    behind, and a refund must be issued against the reference money actually
    arrived on.
    """
    cursor.execute(
        "SELECT payment_ref, payment_dump, date_created FROM payment_collector "
        "WHERE order_id=%s AND status='TXN_SUCCESS' ORDER BY id DESC LIMIT 1",
        (order_id,))
    return cursor.fetchone()


def _latest_status(cursor, order_id):
    cursor.execute("SELECT order_status_name FROM order_status WHERE order_id=%s "
                   "ORDER BY order_status_id DESC LIMIT 1", (order_id,))
    return (cursor.fetchone() or {}).get('order_status_name') or ''


def ledger_refunds(cursor, order_id):
    """Every refund attempt recorded for an order, newest first."""
    cursor.execute(
        "SELECT refund_id, provider_refund_id, amount_minor, currency, status, "
        "refund_type, reason_code, requested_by, service_identity, "
        "idempotency_key, requested_at, completed_at, error_text "
        "FROM order_refunds WHERE order_id=%s ORDER BY refund_id DESC",
        (order_id,))
    return list(cursor.fetchall())


def committed_minor(rows):
    """What is already refunded or in flight, in minor units.

    A FAILED attempt releases its amount; anything else is money the customer
    either has or is going to get, so it cannot be refunded twice.
    """
    return sum(int(r['amount_minor']) for r in rows if r['status'] != FAILED)


def order_lines(cursor, order_id):
    cursor.execute(
        "SELECT order_line_id, product_id, order_quantity, order_total, "
        "fulfillment_status FROM orders WHERE order_id=%s ORDER BY order_line_id",
        (order_id,))
    return list(cursor.fetchall())


def preview(cursor, order_id, provider):
    """Everything a human needs before approving a refund. Writes nothing.

    Amounts come from the provider, not from ``orders.order_total``: what may be
    refunded is what was actually captured, and the two can differ (a coupon
    applied at the gateway, a partial capture, a price edited after the sale).
    """
    row = _site(cursor, order_id)
    if not row.get('line_count'):
        raise RefundRejected('order_not_found', 'no such order', 404)

    site = (row.get('site') or '').lower()
    payment = _successful_payment(cursor, order_id)
    facts = {
        'order_id': order_id,
        'storefront': site or None,
        'is_test': bool(row.get('is_test')),
        'payment_status': 'PAID' if payment else 'UNPAID',
        'payment_ref': payment.get('payment_ref') if payment else None,
        'currency': None,
        'captured_minor': 0,
        'already_refunded_minor': 0,
        'max_refundable_minor': 0,
        'current_status': _latest_status(cursor, order_id),
        'lines': order_lines(cursor, order_id),
        'refunds': ledger_refunds(cursor, order_id),
        'provider': None,
        'provider_state': None,
    }
    if not payment:
        return facts

    captured = provider.payment(payment['payment_ref'])
    facts['provider'] = captured.get('gateway') or 'razorpay'
    facts['provider_state'] = captured.get('status')
    facts['currency'] = captured.get('currency')
    facts['captured_minor'] = int(captured.get('amount') or 0)
    # The provider's tally includes dashboard refunds; gateway ledger rows catch
    # accepted refunds it has not reported yet. UPI refunds are added separately.
    provider_refunded = int(captured.get('amount_refunded') or 0)
    upi_rows = [
        refund for refund in facts['refunds']
        if (refund.get('provider_refund_id') or '').startswith('UPI:')
    ]
    gateway_rows = [
        refund for refund in facts['refunds']
        if not (refund.get('provider_refund_id') or '').startswith('UPI:')
    ]
    upi_minor = sum(int(refund['amount_minor']) for refund in upi_rows
                    if refund['status'] != FAILED)
    gateway_minor = committed_minor(gateway_rows)
    facts['already_refunded_minor'] = max(provider_refunded, gateway_minor) + upi_minor
    facts['max_refundable_minor'] = max(
        0, facts['captured_minor'] - facts['already_refunded_minor'])
    return facts


def refund_type(amount_minor, captured_minor, already_minor):
    return (FULL if amount_minor + already_minor >= captured_minor else PARTIAL)


def validate(facts, amount_minor, currency, reason_code, requested_status=None):
    """Every reason a refund must not reach the provider, checked in order.

    Raises ``RefundRejected``. Returns the refund type when it passes.
    """
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        raise RefundRejected('amount_not_integer',
                             'amount_minor must be an integer in minor units')
    if amount_minor <= 0:
        raise RefundRejected('amount_not_positive', 'amount_minor must be > 0')
    if facts['payment_status'] != 'PAID':
        raise RefundRejected('order_not_paid',
                             'no successful captured payment for this order')
    if not facts['payment_ref']:
        raise RefundRejected('no_payment_reference',
                             'the payment has no provider reference')
    if (facts.get('provider_state') or '') != 'captured':
        raise RefundRejected(
            'payment_not_captured',
            'provider reports the payment as %r, not captured'
            % (facts.get('provider_state') or 'unknown'))
    if not currency or currency != facts['currency']:
        raise RefundRejected(
            'currency_mismatch',
            'this payment was captured in %s; refund it in %s or not at all'
            % (facts['currency'], facts['currency']))
    if amount_minor > facts['max_refundable_minor']:
        raise RefundRejected(
            'amount_exceeds_refundable',
            'at most %d minor units remain refundable'
            % facts['max_refundable_minor'])
    if reason_code not in REASON_CODES:
        raise RefundRejected('bad_reason_code',
                             'reason_code must be one of %s'
                             % ', '.join(REASON_CODES))
    kind = refund_type(amount_minor, facts['captured_minor'],
                       facts['already_refunded_minor'])
    if requested_status and requested_status not in (STATUS_FULL, STATUS_PARTIAL):
        raise RefundRejected('bad_requested_status',
                             'requested_status must be %s or %s'
                             % (STATUS_FULL, STATUS_PARTIAL))
    if requested_status == STATUS_FULL and kind != FULL:
        raise RefundRejected(
            'partial_cannot_be_refunded_status',
            'this is a partial refund; it cannot set the order to %s'
            % STATUS_FULL)
    return kind


def resulting_status(kind):
    """The status a successful refund appends.

    Both names are members of the ``order_status_name`` enum, so a partial
    refund is recorded as a partial rather than silently reading as a full one.
    """
    return STATUS_FULL if kind == FULL else STATUS_PARTIAL


def find_by_key(cursor, idempotency_key):
    cursor.execute(
        "SELECT * FROM order_refunds WHERE idempotency_key=%s", (idempotency_key,))
    return cursor.fetchone()


def _claim(db, cursor, order_id, payment_ref, amount_minor, currency, kind,
           reason_code, comment, idempotency_key, requested_by,
           service_identity, approved_message):
    """Insert the CREATED ledger row that owns this refund attempt.

    The UNIQUE key on ``idempotency_key`` is the real protection against a
    double-click, a retried HTTP request and two operators pressing confirm at
    once: whoever inserts it first is the only one who may call the provider.
    Committed before the provider is called, so a crash mid-call leaves a row
    saying an attempt was made rather than no trace at all.
    """
    cursor.execute(
        "INSERT INTO order_refunds (order_id, payment_ref, amount_minor, "
        "currency, status, refund_type, reason_code, comment, idempotency_key, "
        "requested_by, service_identity, approved_message) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (order_id, payment_ref, amount_minor, currency, CREATED, kind,
         reason_code, comment, idempotency_key, requested_by, service_identity,
         approved_message))
    refund_id = cursor.lastrowid
    db.commit()
    return refund_id


def _is_duplicate_key(exc):
    if exc.args and exc.args[0] in (1062, 1586):
        return True
    return 'Duplicate entry' in str(exc)


def _finish(db, cursor, refund_id, status, provider_refund_id=None,
            response=None, error_text=None):
    cursor.execute(
        "UPDATE order_refunds SET status=%s, provider_refund_id=%s, "
        "provider_response=%s, error_text=%s, "
        "completed_at=CASE WHEN %s IN ('PROCESSED','FAILED') THEN NOW() ELSE NULL END "
        "WHERE refund_id=%s",
        (status, provider_refund_id,
         None if response is None else json.dumps(response, default=str)[:60000],
         (error_text or None) and str(error_text)[:512], status, refund_id))
    db.commit()


def provider_status(state):
    return _PROVIDER_STATE.get((state or '').lower(), PENDING)


def execute(db, order_id, amount_minor, currency, reason_code, comment,
            idempotency_key, requested_by, service_identity, approved_message,
            provider, requested_status=None, logger=None, assessment_id=None):
    """Validate, refund, then record — in that order, and never the reverse.

    A ``RETURN_RECEIVED`` refund executes a return assessment: the amount is
    the assessed proposal, not one the operator typed, and an assessment pays
    out once.

    The order is not moved to ``Refunded`` until the provider has accepted the
    refund: an order that says refunded when no money moved is worse than an
    error message. A provider failure leaves a FAILED ledger row with whatever
    the provider said, so the operator can retry with a new key or reconcile.

    Returns the ledger row as a dict. Never raises for a duplicate key: the
    existing attempt is returned, which is what makes a retry safe.
    """
    cursor = db.cursor()
    existing = find_by_key(cursor, idempotency_key)
    if existing:
        return dict(existing, replayed=True)

    facts = preview(cursor, order_id, provider)
    kind = validate(facts, amount_minor, currency, reason_code, requested_status)
    if reason_code == 'RETURN_RECEIVED' or assessment_id is not None:
        if assessment_id is None:
            raise RefundRejected('assessment_required',
                                 'a return refund names the return assessment '
                                 'whose proposed refund it pays')
        try:
            return_assessment.check_refund_matches(
                return_assessment.get(cursor, assessment_id), order_id,
                amount_minor, currency)
        except return_assessment.AssessmentRejected as exc:
            raise RefundRejected(exc.code, exc.message)

    try:
        refund_id = _claim(
            db, cursor, order_id, facts['payment_ref'], amount_minor, currency,
            kind, reason_code, comment, idempotency_key, requested_by,
            service_identity, approved_message)
    except Exception as exc:
        db.rollback()
        if _is_duplicate_key(exc):
            return dict(find_by_key(cursor, idempotency_key), replayed=True)
        raise

    if logger:
        logger.info(
            "ACTIVITY:REFUND_REQUESTED order:%s refund:%s amount_minor:%s "
            "currency:%s reason:%s type:%s by:%s service:%s key:%s"
            % (order_id, refund_id, amount_minor, currency, reason_code, kind,
               requested_by, service_identity, idempotency_key))

    try:
        result = provider.refund(
            facts['payment_ref'], amount_minor, idempotency_key,
            notes={'order_id': order_id, 'reason_code': reason_code,
                   'idempotency_key': idempotency_key})
    except Exception as exc:
        _finish(db, cursor, refund_id, FAILED, error_text=str(exc))
        if logger:
            logger.error("ACTIVITY:REFUND_FAILED order:%s refund:%s error:%s"
                         % (order_id, refund_id, exc))
        return dict(find_by_key(cursor, idempotency_key), replayed=False)

    status = provider_status(result.get('status'))
    _finish(db, cursor, refund_id, status,
            provider_refund_id=result.get('id'), response=result)

    if status != FAILED:
        _record_outcome(db, cursor, order_id, amount_minor, currency, kind,
                        reason_code, requested_by, result, requested_status)
        if assessment_id is not None:
            return_assessment.mark_executed(cursor, assessment_id, refund_id)
            db.commit()
    if logger:
        logger.info("ACTIVITY:REFUND_%s order:%s refund:%s provider_refund:%s"
                    % (status, order_id, refund_id, result.get('id')))
    return dict(find_by_key(cursor, idempotency_key), replayed=False)


def _upi_key_replay_or_reject(existing):
    if not existing:
        return None
    if (existing.get('provider_refund_id') or '').startswith('UPI:'):
        return dict(existing, replayed=True)
    raise RefundRejected(
        'idempotency_key_reused',
        'this refund key belongs to a gateway refund attempt; start a new '
        'refund for the UPI payment',
        409)


def record_upi(db, order_id, amount_minor, currency, reason_code, comment,
               idempotency_key, requested_by, service_identity, approved_message,
               provider, upi_vpa, utr, paid_on, payee_name,
               requested_status=None, logger=None):
    cursor = db.cursor()
    existing = find_by_key(cursor, idempotency_key)
    if existing:
        return _upi_key_replay_or_reject(existing)

    facts = preview(cursor, order_id, provider)
    kind = validate(facts, amount_minor, currency, reason_code, requested_status)
    if reason_code == 'RETURN_RECEIVED':
        raise RefundRejected(
            'upi_not_for_return_refund',
            'return refunds run through the return assessment and the gateway')

    upi_vpa = str(upi_vpa or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9._-]{2,256}@[A-Za-z][A-Za-z0-9.-]{1,63}',
                        upi_vpa):
        raise RefundRejected('invalid_upi_id', 'enter a valid UPI ID')

    utr = str(utr or '').strip()
    if not re.fullmatch(r'[0-9]{12}', utr):
        raise RefundRejected('invalid_utr', 'UTR must contain exactly 12 digits')

    paid_on = str(paid_on or '').strip()
    try:
        paid_date = date.fromisoformat(paid_on)
    except (TypeError, ValueError):
        raise RefundRejected('invalid_paid_on', 'paid-on date must be YYYY-MM-DD')
    if paid_date.isoformat() != paid_on:
        raise RefundRejected('invalid_paid_on', 'paid-on date must be YYYY-MM-DD')
    today_ist = datetime.now(ZoneInfo('Asia/Kolkata')).date()
    if paid_date > today_ist or paid_date < today_ist - timedelta(days=30):
        raise RefundRejected(
            'invalid_paid_on',
            'paid-on date must be today or within the last 30 days')

    payee_name = str(payee_name or '').strip()
    if not 2 <= len(payee_name) <= 120:
        raise RefundRejected(
            'invalid_payee_name', 'payee name must contain 2 to 120 characters')

    cursor.execute(
        "SELECT order_id FROM order_refunds WHERE provider_refund_id=%s LIMIT 1",
        ('UPI:' + utr,))
    used = cursor.fetchone()
    if used:
        raise RefundRejected(
            'utr_already_used',
            'UTR was already recorded on order %s' % used.get('order_id'))

    try:
        refund_id = _claim(
            db, cursor, order_id, facts['payment_ref'], amount_minor, currency,
            kind, reason_code, comment, idempotency_key, requested_by,
            service_identity, approved_message)
    except Exception as exc:
        db.rollback()
        if _is_duplicate_key(exc):
            replay = _upi_key_replay_or_reject(
                find_by_key(cursor, idempotency_key))
            if replay is not None:
                return replay
        raise

    response = {
        'status': 'processed',
        'method': 'UPI',
        'upi_vpa': upi_vpa,
        'utr': utr,
        'paid_on': paid_on,
        'payee_name': payee_name,
    }
    _finish(db, cursor, refund_id, PROCESSED,
            provider_refund_id='UPI:' + utr, response=response)
    via = 'paid by UPI to %s (%s), UTR %s, on %s' % (
        upi_vpa, payee_name, utr, paid_on)
    _record_outcome(db, cursor, order_id, amount_minor, currency, kind,
                    reason_code, requested_by, response, requested_status,
                    via=via)
    if logger:
        logger.info(
            "ACTIVITY:REFUND_UPI_RECORDED order:%s refund:%s amount_minor:%s "
            "utr:%s by:%s key:%s"
            % (order_id, refund_id, amount_minor, utr, requested_by,
               idempotency_key))
    return dict(find_by_key(cursor, idempotency_key), replayed=False)


def _record_outcome(db, cursor, order_id, amount_minor, currency, kind,
                    reason_code, requested_by, result, requested_status,
                    via=None):
    """History always, then the status the refund earns.

    Written after the provider accepted, in its own transaction, so a failure
    here cannot undo a refund that has already happened — the ledger row is
    already PROCESSED and the operator sees the money as refunded.
    """
    major = minor_to_major(amount_minor)
    history = ('Refund %s %s issued by %s (%s) - %s'
               % (currency, major, requested_by, reason_code,
                  via if via is not None else
                  'provider ref %s' % (result.get('id') or 'pending')))
    add_history(cursor, order_id, history,
                site_from=(_site(cursor, order_id).get('site') or None))
    append_status(cursor, order_id, requested_status or resulting_status(kind),
                  source='ops-refund',
                  note='refund %s %s %s' % (currency, major, reason_code))
    db.commit()


def tracking(db, idempotency_key, provider):
    """Live state of one refund attempt: ours, refreshed from the provider.

    The ledger is the record; the provider is the truth about whether the money
    has landed, and a refund can sit ``pending`` for days on UPI and cards. A
    changed state is written back, so the operator's page and the ledger cannot
    drift apart, and a refund that completes days later still shows as complete
    without anyone watching it.
    """
    cursor = db.cursor()
    row = find_by_key(cursor, idempotency_key)
    if not row:
        raise RefundRejected('refund_not_found', 'no refund for that key', 404)
    out = dict(row)
    # A settled refund is not asked about again, so the state the provider gave
    # when it settled is what the operator is shown - otherwise the page has a
    # provider refund id and no provider state beside it.
    if row.get('provider_response'):
        try:
            out['provider_state'] = (json.loads(row['provider_response'])
                                     or {}).get('status')
        except (ValueError, TypeError, AttributeError):
            pass
    if row.get('provider_refund_id') and row['status'] not in (PROCESSED, FAILED):
        live = provider.refund_status(row['provider_refund_id'])
        out['provider_state'] = live.get('status')
        out['status'] = provider_status(live.get('status'))
        if out['status'] != row['status']:
            _finish(db, cursor, row['refund_id'], out['status'],
                    provider_refund_id=row['provider_refund_id'], response=live)
            # History and status were appended when the provider accepted, and
            # neither is written twice for one refund. A refund that turns out
            # to have failed after acceptance is the exception worth saying out
            # loud: the order still carries the refunded status it was given,
            # so the history has to explain why the money is not there.
            if out['status'] == FAILED:
                add_history(
                    cursor, row['order_id'],
                    'Refund %s %s (provider ref %s) reported FAILED by the '
                    'payment provider after acceptance - reconcile'
                    % (row['currency'], minor_to_major(int(row['amount_minor'])),
                       row['provider_refund_id']),
                    site_from=(_site(cursor, row['order_id']).get('site') or None))
                db.commit()
    return out
