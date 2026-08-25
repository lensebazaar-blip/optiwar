"""The paid-order pipeline, shared by every payment path.

One payment means one set of consequences: the payment is recorded, stock is
deducted atomically, each line becomes ``fulfilled`` or ``refund_pending``, the
sale is logged, depleted products go ``OUT_OF_STOCK``, and the order gains a
``Processed`` status row. Before this module each gateway callback carried its
own copy of that, so Paytm silently skipped stock and sales entirely and both
Razorpay and Paytm rewrote status history with

    UPDATE order_status SET order_status_name='Processed'
    WHERE order_id=%s AND order_status_name<>'Processed'

which turns an order's ``Shipped`` or ``Complete`` row into ``Processed`` when a
callback arrives late. Status is history: it is appended, never overwritten.

Deliberately free of Flask and of the request context so the same code runs from
a browser callback, a gateway webhook and a test against a real database.

    db          any DB-API connection whose cursors return dicts (MySQLdb with
                DictCursor, pymysql with DictCursor)
    logger      optional logging.Logger; messages match the ACTIVITY: format the
                log scrapers already read
"""
import json

PAID_STATUS = 'Processed'

_PROVENANCE_COLUMNS = None

# A payment callback may arrive minutes or days after the fact — Razorpay
# retries a failed webhook for 24 hours. By then the order can legitimately be
# further along, and money arriving cannot walk it backwards.
STATUSES_AFTER_PAID = (
    'Shipped', 'Complete', 'Delivery-assist', 'Returned', 'Refunded',
    'Partially Refunded', 'Processed-Reverse', 'Shipped-Reverse',
)


class DuplicatePayment(Exception):
    """The gateway sent this payment reference before; it was already applied."""


def order_amount_minor(cursor, order_id):
    """Total the customer owes for an order, in the gateway's minor units."""
    cursor.execute(
        "SELECT COALESCE(SUM(order_total), 0) AS total FROM orders WHERE order_id=%s",
        (order_id,))
    row = cursor.fetchone()
    total = (row or {}).get('total') or 0
    return int(round(float(total) * 100))


def order_statuses(cursor, order_id):
    cursor.execute(
        "SELECT order_status_name FROM order_status WHERE order_id=%s", (order_id,))
    return [r['order_status_name'] for r in cursor.fetchall()]


def append_status(cursor, order_id, status_name, source=None, note=None):
    """Append a status row. Never touches the rows already there.

    Returns False when the order already carries this status (so a retried
    callback does not stack duplicate ``Processed`` rows) or when it has already
    moved past paid.
    """
    existing = order_statuses(cursor, order_id)
    if status_name in existing:
        return False
    if status_name == PAID_STATUS and any(s in STATUSES_AFTER_PAID for s in existing):
        return False
    cursor.execute(
        "INSERT INTO order_status (order_status_name, order_id) VALUES (%s, %s)",
        (status_name, order_id))
    _annotate_status(cursor, cursor.lastrowid, source, note)
    return True


def _has_provenance_columns(cursor):
    """Whether ``order_status`` carries the ops provenance columns.

    The ops platform added ``source``/``manual_flag``/``note``/``created_at`` so
    a manual correction is distinguishable from an automated one. A storefront
    on an older schema must still be able to insert a status, so the columns are
    checked rather than assumed — once per process, since a schema does not
    change under a running app.
    """
    global _PROVENANCE_COLUMNS
    if _PROVENANCE_COLUMNS is None:
        cursor.execute("SHOW COLUMNS FROM order_status LIKE 'source'")
        _PROVENANCE_COLUMNS = bool(cursor.fetchall())
    return _PROVENANCE_COLUMNS


def _annotate_status(cursor, status_id, source, note):
    """Stamp source/note on the status row just inserted."""
    if not (source or note) or not _has_provenance_columns(cursor):
        return
    cursor.execute(
        "UPDATE order_status SET source=%s, note=%s WHERE order_status_id=%s",
        (source, note, status_id))


def add_history(cursor, order_id, content, site_from=None):
    if site_from:
        cursor.execute(
            "INSERT INTO order_history (order_history_content, order_id, site_from) "
            "VALUES (%s, %s, %s)", (content, order_id, site_from))
    else:
        cursor.execute(
            "INSERT INTO order_history (order_history_content, order_id) "
            "VALUES (%s, %s)", (content, order_id))


def record_payment(cursor, order_id, payment_ref, payment_dump, status='TXN_SUCCESS'):
    """Insert the payment row. The UNIQUE key on ``payment_ref`` is the
    idempotency gate for the whole pipeline: whoever inserts it first owns the
    stock deduction, the sales log and the notifications.

    Raises DuplicatePayment when this reference is already recorded.
    """
    if not isinstance(payment_dump, str):
        payment_dump = json.dumps(payment_dump, default=str)
    try:
        cursor.execute(
            "INSERT INTO payment_collector (order_id, payment_ref, payment_dump, status) "
            "VALUES (%s, %s, %s, %s)", (order_id, payment_ref, payment_dump, status))
    except Exception as exc:
        if _is_duplicate_key(exc):
            raise DuplicatePayment(payment_ref)
        raise


def _is_duplicate_key(exc):
    """True for MySQL 1062/1586 (duplicate entry), whichever driver raised it."""
    if exc.args and exc.args[0] in (1062, 1586):
        return True
    return 'Duplicate entry' in str(exc)


def deduct_stock(cursor, order_id, currency, site, logger=None):
    """Deduct stock for every line of a paid order, atomically and per line.

    The conditional UPDATE is the concurrency gate: only payments that find
    enough stock win it. A line that loses becomes ``refund_pending`` and is
    reported back to the caller so a human can refund it.
    """
    cursor.execute("SELECT MAX(is_test) AS t FROM orders WHERE order_id=%s", (order_id,))
    row = cursor.fetchone()
    is_test = bool(row and row.get('t'))
    result = {'is_test': is_test, 'fulfilled_count': 0, 'refund_lines': []}
    if is_test:
        return result

    cursor.execute(
        "SELECT order_line_id, product_id, order_quantity, order_total "
        "FROM orders WHERE order_id=%s", (order_id,))
    for line in cursor.fetchall():
        pid = line['product_id']
        qty = line['order_quantity'] or 0
        line_total = line['order_total'] or 0
        cursor.execute(
            "UPDATE products SET product_quantity = product_quantity - %s "
            "WHERE product_id = %s AND product_quantity >= %s", (qty, pid, qty))
        if cursor.rowcount != 1:
            cursor.execute(
                "UPDATE orders SET fulfillment_status='refund_pending' WHERE order_line_id=%s",
                (line['order_line_id'],))
            cursor.execute("SELECT product_code FROM products WHERE product_id=%s", (pid,))
            prod = cursor.fetchone()
            code = (prod.get('product_code') if prod else '') or ''
            result['refund_lines'].append({
                'product_id': pid, 'product_code': code,
                'qty': qty, 'amount': line_total})
            if logger:
                logger.error(
                    "[%s] ACTIVITY:OVERSOLD_REFUND_PENDING order:%s product:%s code:%s "
                    "qty:%s amount:%s currency:%s"
                    % (site, order_id, pid, code, qty, line_total, currency))
            continue

        result['fulfilled_count'] += 1
        cursor.execute(
            "UPDATE orders SET fulfillment_status='fulfilled' WHERE order_line_id=%s",
            (line['order_line_id'],))
        cursor.execute(
            "UPDATE products SET sold_out_at=NOW() "
            "WHERE product_id=%s AND product_quantity<=0 AND sold_out_at IS NULL", (pid,))
        # Lifecycle dual-write: ACTIVE -> OUT_OF_STOCK only (never overrides a
        # manual SEASONAL/DISCONTINUED/ARCHIVED state).
        cursor.execute(
            "UPDATE products SET product_status='OUT_OF_STOCK', status_changed_at=NOW(), "
            "status_changed_by='system-stock', status_reason='auto: stock depleted' "
            "WHERE product_id=%s AND product_quantity<=0 AND product_status='ACTIVE'", (pid,))
        if cursor.rowcount == 1:
            cursor.execute(
                "INSERT INTO product_status_history "
                "(product_id, old_status, new_status, reason, changed_by) "
                "VALUES (%s,'ACTIVE','OUT_OF_STOCK','auto: stock depleted','system-stock')",
                (pid,))
        unit = (float(line_total) / qty) if qty else float(line_total)
        cursor.execute(
            "INSERT INTO sales_log (order_id, product_id, qty, unit_price, currency, site, is_test) "
            "VALUES (%s, %s, %s, %s, %s, %s, 0)",
            (order_id, pid, qty, unit, currency, site))
    return result


def apply_paid_order(db, order_id, payment_ref, payment_dump, currency, site,
                     gateway='razorpay', source='storefront', logger=None):
    """Record a successful payment and everything that follows from it.

    Returns a dict describing what happened; ``applied`` is False only when the
    payment reference was already recorded, in which case nothing is repeated.
    Commits on success, rolls back on failure.
    """
    cursor = db.cursor()
    try:
        record_payment(cursor, order_id, payment_ref, payment_dump)
    except DuplicatePayment:
        db.rollback()
        if logger:
            logger.info("%s duplicate callback ignored order:%s payment_id:%s"
                        % (gateway, order_id, payment_ref))
        return {'applied': False, 'reason': 'duplicate_payment',
                'is_test': False, 'fulfilled_count': 0, 'refund_lines': [],
                'status_appended': False}
    try:
        stock = deduct_stock(cursor, order_id, currency, site, logger=logger)
        appended = append_status(
            cursor, order_id, PAID_STATUS, source=source,
            note='payment received - %s %s' % (gateway, payment_ref))
        add_history(cursor, order_id,
                    'Payment received via %s - %s' % (gateway, payment_ref))
        db.commit()
    except Exception:
        db.rollback()
        raise
    out = {'applied': True, 'reason': '', 'status_appended': appended}
    out.update(stock)
    return out
