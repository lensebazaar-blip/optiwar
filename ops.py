"""Ops/Admin API endpoints for order management and EWS notifications."""

import hmac

from flask import Blueprint, request, jsonify, current_app, session
from .db import get_db
from .notifications import notify_order_shipped, notify_support_ticket_resolved

bp = Blueprint('ops', __name__, url_prefix='/ops')

ADMIN_SESSION_EMAILS = ('admin@ket.ltd', 'admin@optiwar.com', 'lensebazaar@gmail.com')


def _require_ops_auth():
    """Auth for all /ops endpoints (and the read-only Ops Console).

    Accepts either an admin browser session or a Bearer ``OPS_API_TOKEN``.
    There is deliberately NO default token: if ``OPS_API_TOKEN`` is unset the
    Bearer path fails closed and logs an error, so a misconfigured deployment
    denies access rather than silently accepting a published default.
    """
    if session.get('user_email') in ADMIN_SESSION_EMAILS:
        return True
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        ops_token = current_app.config.get('OPS_API_TOKEN')
        if not ops_token:
            current_app.logger.error(
                'OPS_API_TOKEN not configured; Bearer auth for /ops is disabled')
            return False
        if hmac.compare_digest(auth[len('Bearer '):], str(ops_token)):
            return True
    return False


@bp.route('/api/update-order-status', methods=['POST'])
def update_order_status():
    """Update order status and trigger EWS notifications.
    
    POST JSON:
    {
        "order_id": "ABCD-1234",
        "status": "Shipped",
        "tracking_info": "optional tracking URL or number",
        "delivery_days": "5-7"
    }
    """
    if not _require_ops_auth():
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json() or {}
    order_id = data.get('order_id', '').strip()
    new_status = data.get('status', '').strip()
    tracking_info = data.get('tracking_info', '')
    delivery_days = data.get('delivery_days', '5-7')

    if not order_id or not new_status:
        return jsonify({'error': 'order_id and status required'}), 400

    valid_statuses = ['Processed', 'Pending', 'COD not verified', 'COD verified',
                      'Returned', 'Payment Failed', 'Shipped', 'Refunded', 'Partially Refunded']
    if new_status not in valid_statuses:
        return jsonify({'error': f'Invalid status. Valid: {valid_statuses}'}), 400

    db = get_db()
    cursor = db.cursor()

    try:
        # Update order_status
        cursor.execute(
            'UPDATE order_status SET order_status_name=%s WHERE order_id=%s',
            (new_status, order_id)
        )
        if cursor.rowcount == 0:
            return jsonify({'error': f'Order {order_id} not found in order_status'}), 404

        # Log to order_history
        site = data.get('site', 'optiwar.com')
        cursor.execute(
            'INSERT INTO order_history (order_history_content, order_id, site_from) VALUES (%s, %s, %s)',
            (f'Status updated to {new_status}', order_id, site)
        )
        db.commit()

        # Trigger EWS notifications for specific status changes
        ews_results = None
        if new_status == 'Shipped':
            # Get customer info for notification
            cursor.execute("""
                SELECT c.customer_name, c.customer_email, c.customer_phone,
                       ca.delivery_email, ca.delivery_phone, o.site_from
                FROM orders o
                JOIN customers c ON o.customer_id = c.customer_id
                LEFT JOIN customers_address ca ON o.address_id = ca.address_id
                WHERE o.order_id = %s LIMIT 1
            """, (order_id,))
            cust = cursor.fetchone()
            if cust:
                to_email = cust.get('delivery_email') or cust['customer_email']
                to_phone = cust.get('delivery_phone') or cust.get('customer_phone', '')
                cust_name = cust.get('customer_name', 'Customer')
                site_host = cust.get('site_from', 'optiwar.com')
                profile_email = cust['customer_email']

                ews_results = notify_order_shipped(
                    customer_email=to_email,
                    customer_phone=to_phone,
                    customer_name=cust_name,
                    order_id=order_id,
                    site_host=site_host,
                    tracking_info=tracking_info,
                    delivery_days=delivery_days,
                    profile_email=profile_email
                )

        return jsonify({
            'success': True,
            'order_id': order_id,
            'new_status': new_status,
            'ews': ews_results
        })

    except Exception as e:
        db.rollback()
        current_app.logger.error(f"[OPS] update_order_status error: {e}")
        return jsonify({'error': str(e)}), 500


@bp.route('/api/resolve-ticket', methods=['POST'])
def resolve_ticket():
    """Resolve a support ticket and trigger EWS notification.
    
    POST JSON:
    {
        "ticket_id": 70,
        "site_host": "optiwar.com"
    }
    """
    if not _require_ops_auth():
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json() or {}
    ticket_id = data.get('ticket_id')
    site_host = data.get('site_host', 'optiwar.com')

    if not ticket_id:
        return jsonify({'error': 'ticket_id required'}), 400

    db = get_db()
    cursor = db.cursor()

    try:
        # Get ticket info
        cursor.execute("""
            SELECT t.ticket_id, t.customer_name, t.customer_email,
                   c.customer_phone
            FROM tickets t
            LEFT JOIN customers c ON t.customer_email = c.customer_email
            WHERE t.ticket_id = %s LIMIT 1
        """, (ticket_id,))
        ticket = cursor.fetchone()

        if not ticket:
            return jsonify({'error': f'Ticket {ticket_id} not found'}), 404

        # Update ticket status (add resolved_at if column exists)
        try:
            cursor.execute('UPDATE tickets SET status=%s WHERE ticket_id=%s', ('resolved', ticket_id))
            db.commit()
        except Exception:
            db.rollback()
            # status column may not exist yet - that's ok, just send notification

        # Trigger EWS notification
        ews_results = notify_support_ticket_resolved(
            customer_email=ticket.get('customer_email', ''),
            customer_phone=ticket.get('customer_phone', ''),
            customer_name=ticket.get('customer_name', 'Customer'),
            ticket_id=ticket_id,
            site_host=site_host,
        )

        return jsonify({
            'success': True,
            'ticket_id': ticket_id,
            'ews': ews_results
        })

    except Exception as e:
        current_app.logger.error(f"[OPS] resolve_ticket error: {e}")
        return jsonify({'error': str(e)}), 500
