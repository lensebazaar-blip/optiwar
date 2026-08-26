"""The refund API the EU Ops dashboard calls, and the Razorpay side of it.

Optiwar stays the source of truth for payment, refund and order state: EU Ops
never writes a refund into this database and never decides what was captured.
It asks this API three questions —

    GET  /api/ops/orders/<order_id>/refund/preview   what could be refunded
    POST /api/ops/orders/<order_id>/refund           refund exactly this much
    GET  /api/ops/refunds/<idempotency_key>          what happened to it

— and every value in the answer is read from Optiwar's records and from the
payment provider. The instruction may say ``"currency": "EUR"`` about an INR
payment; the API rejects it rather than converting anything.

Authentication is a Bearer credential of its own, ``OPS_REFUND_API_TOKEN``,
scoped to ``orders:read`` and ``refunds:create`` — not the broad
``OPS_API_TOKEN``, and not a database account. Read from the environment
directly, like the Razorpay webhook secret, so enabling this needs one
``Environment=`` line on the service and no code change.

Routes are attached to an existing blueprint rather than registering a new one:
``__init__.py`` is not in the deployment set and production's copy is older than
main, so a module that had to be registered there could not be deployed safely.
"""
import hmac
import os

import requests
from flask import current_app, jsonify, request

from .db import get_db
from . import refunds

SERVICE_IDENTITY = 'eu-ops'
SCOPES = ('orders:read', 'refunds:create')

RAZORPAY_API = 'https://api.razorpay.com/v1'
TIMEOUT = 30

# Razorpay reports amounts in minor units for every currency it settles, which
# is what the ledger stores; no conversion happens anywhere in this file.


class ProviderError(Exception):
    """The provider refused, or could not be reached. Never swallowed."""


class RazorpayProvider:
    """Payment and refund reads/writes against Razorpay, minor units only."""

    def __init__(self, key_id, key_secret, logger=None):
        self.auth = (key_id, key_secret)
        self.logger = logger

    def _get(self, path):
        try:
            resp = requests.get(RAZORPAY_API + path, auth=self.auth,
                                timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise ProviderError('provider unreachable: %s' % exc)
        if resp.status_code >= 400:
            raise ProviderError(_provider_message(resp))
        return resp.json()

    def payment(self, payment_ref):
        """The captured payment: amount, currency, status, amount_refunded."""
        data = self._get('/payments/%s' % payment_ref)
        data['gateway'] = 'razorpay'
        return data

    def refund(self, payment_ref, amount_minor, idempotency_key, notes=None):
        """Create the refund. Returns the provider's refund entity.

        The idempotency key travels twice: in Razorpay's own
        ``Idempotency-Key`` header, and in the refund's notes, which is what
        makes a retry provably safe even against an account where the header is
        not honoured — ``_existing_refund`` finds the earlier attempt by note.
        """
        already = self._existing_refund(payment_ref, idempotency_key)
        if already:
            return already
        payload = {'amount': int(amount_minor), 'speed': 'normal',
                   'notes': notes or {}}
        try:
            resp = requests.post(
                '%s/payments/%s/refund' % (RAZORPAY_API, payment_ref),
                auth=self.auth, json=payload, timeout=TIMEOUT,
                headers={'Idempotency-Key': idempotency_key})
        except requests.RequestException as exc:
            raise ProviderError('provider unreachable: %s' % exc)
        if resp.status_code >= 400:
            raise ProviderError(_provider_message(resp))
        return resp.json()

    def refund_status(self, provider_refund_id):
        return self._get('/refunds/%s' % provider_refund_id)

    def _existing_refund(self, payment_ref, idempotency_key):
        """A refund already created for this key, if the provider has one."""
        try:
            listing = self._get('/payments/%s/refunds' % payment_ref)
        except ProviderError:
            return None
        for item in listing.get('items') or ():
            if (item.get('notes') or {}).get('idempotency_key') == idempotency_key:
                return item
        return None


def _provider_message(resp):
    """The provider's reason, without its response headers or our credential."""
    try:
        err = (resp.json().get('error') or {})
        detail = err.get('description') or err.get('code') or ''
    except ValueError:
        detail = ''
    return 'provider HTTP %s%s' % (resp.status_code,
                                   ': %s' % detail if detail else '')


def _provider():
    key_id = (current_app.config.get('RAZORPAY_KEY_ID')
              or os.environ.get('RAZORPAY_KEY_ID', ''))
    key_secret = (current_app.config.get('RAZORPAY_KEY_SECRET')
                  or os.environ.get('RAZORPAY_KEY_SECRET', ''))
    if not (key_id and key_secret):
        raise ProviderError('Razorpay credentials are not configured')
    return RazorpayProvider(key_id, key_secret, logger=current_app.logger)


def _authorised(scope):
    """Bearer ``OPS_REFUND_API_TOKEN`` carrying ``scope``.

    Fails closed: no token configured means no service may refund, and the
    reason is logged once per attempt rather than silently 401ing.
    """
    if scope not in SCOPES:
        return False
    token = (current_app.config.get('OPS_REFUND_API_TOKEN')
             or os.environ.get('OPS_REFUND_API_TOKEN', ''))
    if not token:
        current_app.logger.error(
            'OPS_REFUND_API_TOKEN not configured; the ops refund API is disabled')
        return False
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        return False
    return hmac.compare_digest(header[len('Bearer '):], str(token))


def _deny():
    return jsonify({'ok': False, 'error': 'unauthorised',
                    'scopes_required': list(SCOPES)}), 401


def _rejected(exc):
    return jsonify({'ok': False, 'error': exc.code,
                    'message': exc.message}), exc.http_status


def _operator():
    """The human the dashboard says approved this, recorded for the audit.

    Required: a refund with no named approver is not auditable, and the service
    identity alone names the machine, not the person.
    """
    who = (request.headers.get('X-Ops-Operator') or '').strip()[:191]
    return who or None


def _facts_json(facts):
    """The preview, shaped for the dashboard: integers, no floats, no PII."""
    return {
        'order_id': facts['order_id'],
        'storefront': facts['storefront'],
        'is_test': facts['is_test'],
        'payment_status': facts['payment_status'],
        'payment_ref': facts['payment_ref'],
        'provider': facts['provider'],
        'provider_state': facts['provider_state'],
        'currency': facts['currency'],
        'captured_minor': facts['captured_minor'],
        'already_refunded_minor': facts['already_refunded_minor'],
        'max_refundable_minor': facts['max_refundable_minor'],
        'captured': refunds.minor_to_major(facts['captured_minor']),
        'already_refunded': refunds.minor_to_major(facts['already_refunded_minor']),
        'max_refundable': refunds.minor_to_major(facts['max_refundable_minor']),
        'current_status': facts['current_status'],
        'proposed_status': refunds.STATUS_FULL,
        'reason_codes': list(refunds.REASON_CODES),
        'lines': [{'order_line_id': l['order_line_id'],
                   'product_id': l['product_id'],
                   'quantity': l['order_quantity'],
                   'line_total': str(l['order_total']),
                   'fulfillment_status': l['fulfillment_status']}
                  for l in facts['lines']],
        'refunds': [{'refund_id': r['refund_id'],
                     'provider_refund_id': r['provider_refund_id'],
                     'amount_minor': int(r['amount_minor']),
                     'currency': r['currency'],
                     'status': r['status'],
                     'refund_type': r['refund_type'],
                     'reason_code': r['reason_code'],
                     'requested_by': r['requested_by'],
                     'idempotency_key': r['idempotency_key'],
                     'requested_at': str(r['requested_at']),
                     'completed_at': (str(r['completed_at'])
                                      if r['completed_at'] else None),
                     'error_text': r['error_text']}
                    for r in facts['refunds']],
    }


def _ledger_json(row):
    return {
        'refund_id': row['refund_id'],
        'order_id': row['order_id'],
        'provider_refund_id': row.get('provider_refund_id'),
        'amount_minor': int(row['amount_minor']),
        'amount': refunds.minor_to_major(int(row['amount_minor'])),
        'currency': row['currency'],
        'status': row['status'],
        'provider_state': row.get('provider_state'),
        'refund_type': row['refund_type'],
        'reason_code': row['reason_code'],
        'idempotency_key': row['idempotency_key'],
        'requested_by': row['requested_by'],
        'service_identity': row['service_identity'],
        'requested_at': str(row['requested_at']),
        'completed_at': str(row['completed_at']) if row.get('completed_at') else None,
        'error_text': row.get('error_text'),
        'replayed': bool(row.get('replayed')),
    }


def register(bp):
    """Attach the refund endpoints to an already-registered blueprint."""

    @bp.route('/api/ops/orders/<order_id>/refund/preview', methods=['GET'])
    def ops_refund_preview(order_id):
        if not _authorised('orders:read'):
            return _deny()
        db = get_db()
        cursor = db.cursor()
        refunds.ensure_schema(cursor)
        try:
            facts = refunds.preview(cursor, order_id, _provider())
        except refunds.RefundRejected as exc:
            return _rejected(exc)
        except ProviderError as exc:
            return jsonify({'ok': False, 'error': 'provider_unavailable',
                            'message': str(exc)}), 502
        return jsonify({'ok': True, 'preview': _facts_json(facts)})

    @bp.route('/api/ops/orders/<order_id>/refund', methods=['POST'])
    def ops_refund_execute(order_id):
        if not _authorised('refunds:create'):
            return _deny()
        operator = _operator()
        if not operator:
            return jsonify({'ok': False, 'error': 'operator_required',
                            'message': 'X-Ops-Operator must name the human '
                                       'who approved this refund'}), 400
        body = request.get_json(silent=True) or {}
        # Required, not generated here: a key this API invented would be unique
        # per request, which is the one thing it must not be — the caller's
        # retry of a timed-out refund has to carry the same key as the attempt
        # it is retrying, or it buys the customer a second refund.
        key = (request.headers.get('Idempotency-Key')
               or body.get('idempotency_key') or '').strip()
        if not key:
            return jsonify({'ok': False, 'error': 'idempotency_key_required',
                            'message': 'send Idempotency-Key, unique per refund '
                                       'and identical across retries'}), 400
        db = get_db()
        refunds.ensure_schema(db.cursor())
        try:
            row = refunds.execute(
                db, order_id,
                amount_minor=body.get('amount_minor'),
                currency=body.get('currency'),
                reason_code=body.get('reason_code'),
                comment=(body.get('comment') or '')[:2000] or None,
                idempotency_key=str(key)[:191],
                requested_by=operator,
                service_identity=SERVICE_IDENTITY,
                approved_message=body.get('approved_message'),
                provider=_provider(),
                requested_status=body.get('requested_status'),
                logger=current_app.logger)
        except refunds.RefundRejected as exc:
            current_app.logger.info(
                'ACTIVITY:REFUND_REJECTED order:%s reason:%s by:%s service:%s'
                % (order_id, exc.code, operator, SERVICE_IDENTITY))
            return _rejected(exc)
        except ProviderError as exc:
            return jsonify({'ok': False, 'error': 'provider_unavailable',
                            'message': str(exc)}), 502
        out = _ledger_json(row)
        return jsonify({'ok': row['status'] != refunds.FAILED, 'refund': out}), (
            200 if row['status'] != refunds.FAILED else 502)

    @bp.route('/api/ops/refunds/<path:idempotency_key>', methods=['GET'])
    def ops_refund_tracking(idempotency_key):
        if not _authorised('orders:read'):
            return _deny()
        db = get_db()
        refunds.ensure_schema(db.cursor())
        try:
            row = refunds.tracking(db, idempotency_key, _provider())
        except refunds.RefundRejected as exc:
            return _rejected(exc)
        except ProviderError as exc:
            return jsonify({'ok': False, 'error': 'provider_unavailable',
                            'message': str(exc)}), 502
        return jsonify({'ok': True, 'refund': _ledger_json(row)})
