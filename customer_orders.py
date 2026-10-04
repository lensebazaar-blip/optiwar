"""The orders a customer is shown in their account.

A row in ``orders`` is written the moment checkout starts, before any money
moves. Most of those rows are attempts the gateway never came back from, and
they used to appear in the customer's order history as PENDING, one card per
attempt, next to the orders they actually paid for. The account panel shows an
order only when ``paid_orders.payment_state`` calls it paid — a successful
``payment_collector`` row or a status ops moved it to deliberately. Unpaid
attempts stay in the database for Ops; they are not the customer's orders.

A test purchase (``/test-checkout``) writes a ``TXN_SUCCESS`` row whose dump
says ``TEST_BUY`` and a ``Processed`` status without any money moving. It is
flagged ``orders.is_test``; older test orders predate the flag and carry only
the dump. Neither is a customer's order, so both are left out of the panel.

``order_status`` is append-only history (one row per status), so the panel
reads the latest row per order rather than joining every row, which used to
repeat each item once per status the order had passed through.
"""
from collections import OrderedDict

try:
    from .paid_orders import payment_state
    from . import reship, reverse_pickup
except ImportError:  # loaded standalone by the tests
    from paid_orders import payment_state
    import reship
    import reverse_pickup

# Latest status row per order and whether any successful payment exists, as
# columns on each order line. ``payment_collector`` is joined in a subquery so
# an order with two payment rows does not double its lines.
ORDER_LINES_SQL = (
    "SELECT o.order_id, o.order_quantity, o.order_total, o.date_created, "
    "o.site_from, "
    "(SELECT os.order_status_name FROM order_status os WHERE os.order_id=o.order_id "
    " ORDER BY os.order_status_id DESC LIMIT 1) AS order_status_name, "
    "p.product_name, p.product_image, p.product_special_price, p.product_code, "
    "p.product_category, "
    "rc.right_eye, rc.left_eye, rc.recommendations, "
    "rc.addon_1_name, rc.addon_1_price, rc.addon_2_name, rc.addon_2_price, "
    "rc.addon_3_name, rc.addon_3_price, "
    "pc.payment_date, "
    "(o.is_test = 1 OR EXISTS (SELECT 1 FROM payment_collector t "
    "   WHERE t.order_id = o.order_id AND t.payment_dump LIKE '%%TEST_BUY%%')) "
    "AS is_test_order "
    "FROM orders o "
    "JOIN products p ON o.product_id = p.product_id "
    "LEFT JOIN rx_collector rc ON rc.rx_id = o.rx_id "
    "LEFT JOIN (SELECT order_id, MIN(date_created) AS payment_date "
    "           FROM payment_collector WHERE status='TXN_SUCCESS' "
    "             AND payment_dump NOT LIKE '%%TEST_BUY%%' "
    "           GROUP BY order_id) pc ON pc.order_id = o.order_id "
)

# Customer-facing stage for each status name ops uses. ``step`` is the
# position on the Confirmed → Shipped → Delivered track (0 = off-track).
STAGES = {
    'Processed': ('Confirmed', 'confirmed', 1),
    'Shipped': ('Shipped', 'shipped', 2),
    'Delivery-assist': ('Out for delivery', 'shipped', 2),
    'Complete': ('Delivered', 'delivered', 3),
    'Returned': ('Returned', 'returned', 0),
    'Refunded': ('Refunded', 'refunded', 0),
    'Partially Refunded': ('Partially refunded', 'refunded', 0),
    'Processed-Reverse': ('Return in progress', 'returned', 0),
    'Shipped-Reverse': ('Return in progress', 'returned', 0),
}

TRACK = ('Confirmed', 'Shipped', 'Delivered')


def stage(status_name):
    """``(label, tone, step)`` a customer understands for an ops status."""
    name = (status_name or '').strip()
    if name in STAGES:
        return STAGES[name]
    return (name or 'Confirmed', 'confirmed', 1)


def customer_orders(rows):
    """Group order lines by order and keep only the orders that are paid.

    ``rows`` are ``ORDER_LINES_SQL`` results, newest first. Returns a list of
    dicts the profile template renders; an order whose payment state is
    ``pending`` or ``failed`` is not in it, and neither is a test order
    (``is_test_order`` truthy).
    """
    grouped = OrderedDict()
    for row in rows:
        if row.get('is_test_order'):
            continue
        oid = row['order_id']
        order = grouped.get(oid)
        if order is None:
            status = row.get('order_status_name')
            state = payment_state(row.get('payment_date') is not None, status)
            label, tone, step = stage(status)
            order = grouped[oid] = {
                'order_id': oid,
                'date_created': row.get('date_created'),
                'site_from': row.get('site_from'),
                'order_status_name': status,
                'payment_state': state,
                'stage_label': label,
                'stage_tone': tone,
                'stage_step': step,
                'track': TRACK,
                'payment_date': row.get('payment_date'),
                'items': [],
                'grand_total': 0,
                'item_count': 0,
            }
        order['items'].append(row)
        order['grand_total'] += (row.get('order_total') or 0)
        order['item_count'] += (row.get('order_quantity') or 0)
    return [o for o in grouped.values() if o['payment_state'] == 'paid']


RESHIP_LABELS = {
    'RETURNING_TO_OPS': ('Returning to Optiwar', 'returned'),
    'RETURNED_TO_OPS': ('Returned to Optiwar', 'returned'),
    'RESHIP_PAID': ('Reshipment paid', 'confirmed'),
    'RESHIPPED': ('Reshipped', 'shipped'),
    'ABANDONED': ('Abandoned', 'returned'),
}


def attach_reship(orders, reship_rows, host, environ=None, shipments=None, now=None):
    """Give each order its reship card, from server state only.

    ``reship_rows`` is ``reship.for_customer``'s ``{order_id: row}``;
    ``shipments`` is ``reship.shipments_for_orders``'s ``{order_id: (awb,
    courier)}`` so the card can show the parcel being returned. An order
    gets ``order['reship']`` (``reship.public_view``) only when the workflow
    is open for it on this host — so on .com nothing is attached and the
    template has nothing to draw. The header label follows the reship state.
    """
    for order in orders:
        order['reship'] = None
        oid = order['order_id']
        if not reship.workflow_open(host, order.get('site_from'), oid, environ):
            continue
        row = reship_rows.get(oid)
        view = reship.public_view(row, order.get('order_status_name'),
                                  shipment=(shipments or {}).get(oid), now=now,
                                  environ=environ)
        if view is None:
            continue
        order['reship'] = view
        label, tone = RESHIP_LABELS[view['state']]
        order['stage_label'], order['stage_tone'] = label, tone
        order['stage_step'] = 2 if view['state'] == 'RESHIPPED' else 0
    return orders


REQUEST_LABELS = ('SUBMITTED', 'INFO_REQUESTED', 'APPROVED_FEE_DUE', 'APPROVED')
CLOSING_LABELS = {'RECEIVED': 'Return: parcel received', 'DEFECT_CONFIRMED': 'Return: defect confirmed',
                  'FEE_REFUNDED': 'Return: fee refunded', 'SENDING_BACK': 'Return: on its way back',
                  'AWAITING_REPLY': 'Return: your reply needed', 'SHIPPED_TO_CUSTOMER': 'Return: shipped to you',
                  'COMPLETED': 'Return complete', 'ABANDONED': 'Return closed: unclaimed'}


def attach_reverse_pickup(orders, pickup_rows, request_cards=None):
    """Give each order its reverse-pickup card from ``reverse_pickup.
    latest_for_customer``'s ``{order_id: row}``, and its return-request card
    from ``return_request.customer_cards``. A booked pickup or an open request
    sets the header label; a cancelled pickup is shown but does not, and a
    pickup closed with its case is not shown at all."""
    for order in orders:
        view = reverse_pickup.public_view((pickup_rows or {}).get(order['order_id']))
        if view and view['state'] == reverse_pickup.ST_CLOSED:
            view = None
        order['reverse_pickup'] = view
        card = (request_cards or {}).get(order['order_id'])
        order['return_request'] = card
        if card and card['state'] in REQUEST_LABELS:
            order['stage_label'], order['stage_tone'] = 'Return requested', 'returned'
        if card and card['state'] in CLOSING_LABELS:
            # The pickup card is history once Optiwar holds the product.
            order['reverse_pickup'] = view = None
            order['stage_label'], order['stage_tone'] = CLOSING_LABELS[card['state']], 'returned'
        if view and view['state'] == reverse_pickup.ST_BOOKED:
            order['stage_label'], order['stage_tone'] = 'Reverse pickup scheduled', 'returned'
    return orders
