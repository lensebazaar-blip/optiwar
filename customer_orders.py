"""The orders a customer is shown in their account.

A row in ``orders`` is written the moment checkout starts, before any money
moves. Most of those rows are attempts the gateway never came back from, and
they used to appear in the customer's order history as PENDING, one card per
attempt, next to the orders they actually paid for. The account panel shows an
order only when ``paid_orders.payment_state`` calls it paid — a successful
``payment_collector`` row, a cash-on-delivery status, or a status ops moved it
to deliberately. Unpaid attempts stay in the database for Ops; they are not
the customer's orders.

``order_status`` is append-only history (one row per status), so the panel
reads the latest row per order rather than joining every row, which used to
repeat each item once per status the order had passed through.
"""
from collections import OrderedDict

try:
    from .paid_orders import payment_state
except ImportError:  # loaded standalone by the tests
    from paid_orders import payment_state

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
    "pc.payment_date "
    "FROM orders o "
    "JOIN products p ON o.product_id = p.product_id "
    "LEFT JOIN rx_collector rc ON rc.rx_id = o.rx_id "
    "LEFT JOIN (SELECT order_id, MIN(date_created) AS payment_date "
    "           FROM payment_collector WHERE status='TXN_SUCCESS' "
    "           GROUP BY order_id) pc ON pc.order_id = o.order_id "
)

# Customer-facing stage for each status name ops uses. ``step`` is the
# position on the Confirmed → Shipped → Delivered track (0 = off-track).
STAGES = {
    'Processed': ('Confirmed', 'confirmed', 1),
    'COD not verified': ('Confirmed · pay on delivery', 'confirmed', 1),
    'COD verfieid': ('Confirmed · pay on delivery', 'confirmed', 1),
    'COD verified': ('Confirmed · pay on delivery', 'confirmed', 1),
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
    ``pending`` or ``failed`` is not in it.
    """
    grouped = OrderedDict()
    for row in rows:
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
                'pay_on_delivery': status in (
                    'COD not verified', 'COD verfieid', 'COD verified'),
                'items': [],
                'grand_total': 0,
                'item_count': 0,
            }
        order['items'].append(row)
        order['grand_total'] += (row.get('order_total') or 0)
        order['item_count'] += (row.get('order_quantity') or 0)
    return [o for o in grouped.values() if o['payment_state'] == 'paid']
