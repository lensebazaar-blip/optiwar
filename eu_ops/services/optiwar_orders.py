"""Optiwar order service — queries the optiwar2 database."""
import json
import logging
from database import ow_cursor, db_cursor, ow_ops_cursor, get_ow_connection
from services import invoice_series, ow_rx

log = logging.getLogger("optiwar_orders")

# Map optiwar status names to a numeric-like identifier for consistency
OW_STATUS_MAP = {
    "Processed": 15,
    "Pending": 1,
    "COD not verified": 20,
    "COD verfieid": 17,
    "Returned": 12,
    "Payment Failed": 10,
    "Shipped": 3,
    "Refunded": 11,
    "COD verified": 17,
    "Complete": 5,
    "Partially Refunded": 11,
    "Processed-Reverse": 15,
    "Shipped-Reverse": 3,
    "Delivery-assist": 3,
}

# An optiwar order's current status is its newest order_status row. History is
# append-only, so the alphabetically largest name is not the latest one --
# MAX() would call a Pending->Processed->Shipped->Complete order "Shipped".
LATEST_STATUS_SQL = """
                SELECT s.order_id, s.order_status_name
                FROM order_status s
                JOIN (SELECT order_id, MAX(order_status_id) AS latest_id
                      FROM order_status GROUP BY order_id) newest
                  ON newest.order_id = s.order_id
                 AND newest.latest_id = s.order_status_id
"""


def list_orders(page: int = 1, per_page: int = 20, status: str = None, search: str = None, show_archived: bool = False):
    """List optiwar orders with aggregation (since orders table is line-item based)."""
    with ow_cursor() as cur:
        where = []
        params = []

        if not show_archived:
            where.append("o.archived = 0")

        # Exclude Pending (incomplete/unpaid) unless explicitly filtered
        if status:
            where.append("os_agg.order_status_name = %s")
            params.append(status)
        elif not show_archived:
            where.append("os_agg.order_status_name != 'Pending'")

        if search:
            search_clean = search.strip()
            if search_clean.upper().startswith("OW-"):
                search_clean = search_clean[3:]
            where.append(
                "(o.order_id LIKE %s OR c.customer_name LIKE %s OR c.customer_email LIKE %s OR c.customer_phone LIKE %s)"
            )
            params.extend([f"%{search_clean}%", f"%{search_clean}%", f"%{search_clean}%", f"%{search_clean}%"])

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        # Count distinct orders
        count_sql = f"""
            SELECT COUNT(DISTINCT o.order_id) as cnt
            FROM orders o
            LEFT JOIN customers c ON o.customer_id = c.customer_id
            LEFT JOIN ({LATEST_STATUS_SQL}) os_agg ON o.order_id = os_agg.order_id
            {where_sql}
        """
        cur.execute(count_sql, params)
        total = cur.fetchone()["cnt"]

        # Fetch orders with aggregation
        list_sql = f"""
            SELECT o.order_id,
                   MAX(o.date_created) as date_added,
                   SUM(o.order_total) as total,
                   COUNT(*) as line_count,
                   MAX(c.customer_name) as firstname,
                   '' as lastname,
                   MAX(c.customer_email) as email,
                   MAX(c.customer_phone) as telephone,
                   MAX(os_agg.order_status_name) as status_name,
                   MAX(pc.status) as payment_status,
                   MAX(o.archived) as archived
            FROM orders o
            LEFT JOIN customers c ON o.customer_id = c.customer_id
            LEFT JOIN ({LATEST_STATUS_SQL}) os_agg ON o.order_id = os_agg.order_id
            LEFT JOIN payment_collector pc ON o.order_id = pc.order_id
            {where_sql}
            GROUP BY o.order_id
            ORDER BY MAX(o.date_created) DESC
            LIMIT %s OFFSET %s
        """
        cur.execute(list_sql, params + [per_page, (page - 1) * per_page])
        orders = cur.fetchall()

        for o in orders:
            o["total_inr"] = float(o["total"] or 0)
            o["source"] = "optiwar"
            o["order_status_id"] = OW_STATUS_MAP.get(o.get("status_name"), 0)

        return {
            "orders": orders,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
        }


PRODUCT_IMAGE_BASE = "https://optiwar.com/static/"


def product_image_urls(product_image: str) -> list:
    """Absolute optiwar.com URLs for the comma-separated './catalog/...' paths in products.product_image."""
    out = []
    for part in (product_image or "").split(","):
        part = part.strip().lstrip(".").lstrip("/")
        if part:
            out.append(PRODUCT_IMAGE_BASE + part)
    return out


def get_order(order_id: str):
    """Get full optiwar order detail."""
    with ow_cursor() as cur:
        # Get order lines with product and customer info
        cur.execute("""
            SELECT o.order_line_id, o.order_id, o.date_created, o.order_quantity,
                   o.order_total, o.product_id, o.rx_id, o.customer_id, o.site_from,
                   c.customer_name, c.customer_email, c.customer_phone,
                   p.product_name, p.product_code, p.product_price,
                   p.product_special_price, p.product_category, p.product_color,
                   p.product_size,
                   p.product_style, p.product_material, p.product_type, p.box_number,
                   p.product_image
            FROM orders o
            LEFT JOIN customers c ON o.customer_id = c.customer_id
            LEFT JOIN products p ON o.product_id = p.product_id
            WHERE o.order_id = %s
            ORDER BY o.order_line_id
        """, (order_id,))
        lines = cur.fetchall()

        if not lines:
            return None

        first = lines[0]

        # Get customer address
        cur.execute("""
            SELECT address, state, zipcode, country
            FROM customers_address
            WHERE customer_id = %s
            ORDER BY address_id DESC LIMIT 1
        """, (first["customer_id"],))
        addr = cur.fetchone() or {}

        # Get order status
        cur.execute("""
            SELECT order_status_name FROM order_status
            WHERE order_id = %s
            ORDER BY order_status_id DESC LIMIT 1
        """, (order_id,))
        status_row = cur.fetchone()
        status_name = status_row["order_status_name"] if status_row else "Unknown"

        # Get payment info
        cur.execute("""
            SELECT status, date_created FROM payment_collector
            WHERE order_id = %s
            ORDER BY date_created DESC LIMIT 1
        """, (order_id,))
        payment = cur.fetchone() or {}

        # Get order history
        cur.execute("""
            SELECT order_history_content as comment, order_history_date as date_added
            FROM order_history
            WHERE order_id = %s
            ORDER BY order_history_date DESC
        """, (order_id,))
        history = cur.fetchall()

        # Build products list with rx info
        products = []
        grand_total = 0
        for line in lines:
            rx_info = None
            if line.get("rx_id"):
                cur.execute("""
                    SELECT recommendations, recommendation_price,
                           right_eye, left_eye, super_recommendations,
                           addon_1_name, addon_1_price,
                           addon_2_name, addon_2_price,
                           addon_3_name, addon_3_price
                    FROM rx_collector WHERE rx_id = %s
                """, (line["rx_id"],))
                rx_info = cur.fetchone()

            products.append({
                "order_product_id": line["order_line_id"],
                "rx_id": line.get("rx_id"),
                "product_id": line.get("product_id"),
                "image_urls": product_image_urls(line.get("product_image")),
                "name": line.get("product_name") or "Unknown Product",
                "model": line.get("product_code") or "",
                "quantity": line["order_quantity"],
                "price": float(line["order_total"]) / max(line["order_quantity"], 1),
                "total": float(line["order_total"]),
                "rx": rx_info,
                "options": [],
                "product_color": line.get("product_color"),
                "size": line.get("product_size") or "",
                "product_style": line.get("product_style"),
                "product_type": line.get("product_type"),
                "box_number": line.get("box_number"),
            })
            grand_total += float(line["order_total"])

        # Build normalized order object
        order = {
            "order_id": order_id,
            "source": "optiwar",
            "date_added": first["date_created"],
            "firstname": first.get("customer_name") or "",
            "lastname": "",
            "email": first.get("customer_email") or "",
            "telephone": first.get("customer_phone") or "",
            "status_name": status_name,
            "order_status_id": OW_STATUS_MAP.get(status_name, 0),
            "payment_method": "TXN_SUCCESS" if payment.get("status") == "TXN_SUCCESS" else (payment.get("status") or "N/A"),
            "shipping_method": "Standard Shipping",
            # Address
            "shipping_firstname": first.get("customer_name") or "",
            "shipping_lastname": "",
            "shipping_company": "",
            "shipping_address_1": addr.get("address") or "",
            "shipping_address_2": "",
            "shipping_city": "",
            "shipping_postcode": addr.get("zipcode") or "",
            "shipping_country": addr.get("country") or "",
            "shipping_country_code": "IN",
            "shipping_zone": addr.get("state") or "",
            # Payment address (same)
            "payment_address_1": addr.get("address") or "",
            "payment_address_2": "",
            "payment_city": "",
            "payment_postcode": addr.get("zipcode") or "",
            "payment_country": addr.get("country") or "",
            # Totals
            "total": grand_total,
            "total_inr": grand_total,
            "currency_code": "INR",
            "currency_value": 1.0,
            # Products, history
            "products": products,
            "totals": [
                {"title": "Sub-Total", "text": f"₹{grand_total:,.2f}", "value": grand_total},
                {"title": "Total", "text": f"₹{grand_total:,.2f}", "value": grand_total},
            ],
            "history": [{"date_added": h["date_added"], "status_name": h.get("comment", ""), "comment": h.get("comment", ""), "notify": 0} for h in history],
            "awbs": [],
            "approvals": [],
            "site_from": first.get("site_from") or "",
            "site_domain": "optiwar.in" if first.get("site_from") == "in.optiwar.com" else "optiwar.com",
            "invoice_prefix": "OW/",
            "invoice_no": order_id,
        }

    # Attach any recorded free lens upgrade (outside the ow_cursor block so its
    # own cursor/table-ensure runs cleanly).
    order["upgrade"] = get_upgrade(order_id)
    return order


def get_order_counts_by_status():
    """Get order counts grouped by status for dashboard (excludes archived)."""
    with ow_cursor() as cur:
        cur.execute("""
            SELECT os_agg.order_status_name as status_name, COUNT(DISTINCT o.order_id) as cnt
            FROM orders o
            LEFT JOIN (""" + LATEST_STATUS_SQL + """) os_agg ON o.order_id = os_agg.order_id
            WHERE o.archived = 0 AND os_agg.order_status_name != 'Pending'
            GROUP BY os_agg.order_status_name
            ORDER BY cnt DESC
        """)
        rows = cur.fetchall()
        result = []
        for r in rows:
            sname = r["status_name"] or "Unknown"
            result.append({
                "status_name": sname,
                "order_status_id": OW_STATUS_MAP.get(sname, 0),
                "cnt": r["cnt"],
            })
        return result


def archive_order(order_id: str):
    """Archive an optiwar order (hide from main list)."""
    with ow_cursor() as cur:
        cur.execute("UPDATE orders SET archived = 1 WHERE order_id = %s", (order_id,))
        return cur.rowcount > 0


def unarchive_order(order_id: str):
    """Unarchive an optiwar order (show in main list again)."""
    with ow_cursor() as cur:
        cur.execute("UPDATE orders SET archived = 0 WHERE order_id = %s", (order_id,))
        return cur.rowcount > 0


def _ensure_deleted_orders_table():
    """Create the deleted-order snapshot table (optiwar2, ops-owned).
    A hard delete cascades order_status/order_history away, so the full row set is
    stored here as JSON first — that is the only way a deletion can be reviewed
    or reconstructed afterwards."""
    with ow_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_deleted_orders (
                id INT AUTO_INCREMENT PRIMARY KEY,
                order_id VARCHAR(64) NOT NULL,
                snapshot LONGTEXT NOT NULL,
                reason VARCHAR(255) NULL,
                deleted_by INT NULL,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_ow_deleted_order (order_id)
            )
        """)


def archive_order_audited(order_id: str, reason: str = "", actor_id=None):
    """Archive an order and record why, in order_history (ASCII only - latin1 table)."""
    if not archive_order(order_id):
        return False, "order not found or already archived"
    note = "Archived by ops"
    if reason:
        note += " - " + reason
    add_history(order_id, note[:255])
    return True, ""


def delete_order(order_id: str, reason: str = "", actor_id=None):
    """Hard-delete an optiwar order after snapshotting every related row.

    payment_collector has an ON DELETE RESTRICT foreign key, so its rows must go
    first; order_status/order_history then cascade with the order lines.
    Returns (ok, error)."""
    try:
        _ensure_deleted_orders_table()
        with ow_cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            lines = cur.fetchall()
            if not lines:
                return False, "order not found"

            cur.execute("SELECT * FROM order_status WHERE order_id = %s", (order_id,))
            statuses = cur.fetchall()
            cur.execute("SELECT * FROM order_history WHERE order_id = %s", (order_id,))
            history = cur.fetchall()
            cur.execute("SELECT * FROM payment_collector WHERE order_id = %s", (order_id,))
            payments = cur.fetchall()

            rx_ids = [l["rx_id"] for l in lines if l.get("rx_id")]
            rx_rows = []
            if rx_ids:
                ph = ",".join(["%s"] * len(rx_ids))
                cur.execute(f"SELECT * FROM rx_collector WHERE rx_id IN ({ph})", rx_ids)
                rx_rows = cur.fetchall()

            snapshot = json.dumps({
                "orders": lines,
                "order_status": statuses,
                "order_history": history,
                "payment_collector": payments,
                "rx_collector": rx_rows,
            }, default=str)
            cur.execute(
                "INSERT INTO ow_deleted_orders (order_id, snapshot, reason, deleted_by) "
                "VALUES (%s, %s, %s, %s)",
                (order_id, snapshot, (reason or "")[:255], actor_id))

            cur.execute("DELETE FROM payment_collector WHERE order_id = %s", (order_id,))
            cur.execute("DELETE FROM orders WHERE order_id = %s", (order_id,))
            deleted = cur.rowcount

            # Drop prescriptions left orphaned by this order only.
            for rx_id in rx_ids:
                cur.execute("SELECT COUNT(*) AS n FROM orders WHERE rx_id = %s", (rx_id,))
                if (cur.fetchone() or {}).get("n", 1) == 0:
                    cur.execute("DELETE FROM rx_collector WHERE rx_id = %s", (rx_id,))

        log.info("Optiwar order %s deleted (%s line(s)) by %s: %s",
                 order_id, deleted, actor_id, reason)
        return True, ""
    except Exception as e:
        log.error("Failed to delete Optiwar order %s: %s", order_id, e)
        return False, str(e)


def get_deleted_orders(limit: int = 100):
    """Return recent deletion snapshots (newest first)."""
    try:
        _ensure_deleted_orders_table()
        with ow_cursor() as cur:
            cur.execute(
                "SELECT id, order_id, reason, deleted_by, deleted_at FROM ow_deleted_orders "
                "ORDER BY id DESC LIMIT %s", (int(limit),))
            return cur.fetchall()
    except Exception as e:
        log.warning("get_deleted_orders failed: %s", e)
        return []


def bulk_archive_orders(order_ids: list):
    """Archive multiple optiwar orders at once."""
    if not order_ids:
        return 0
    with ow_cursor() as cur:
        placeholders = ",".join(["%s"] * len(order_ids))
        cur.execute(f"UPDATE orders SET archived = 1 WHERE order_id IN ({placeholders})", order_ids)
        return cur.rowcount


def get_status_list():
    """Return the list of optiwar statuses."""
    return [
        {"name": "Pending", "id": "Pending"},
        {"name": "Processed", "id": "Processed"},
        {"name": "Shipped", "id": "Shipped"},
        {"name": "Returned", "id": "Returned"},
        {"name": "Payment Failed", "id": "Payment Failed"},
        {"name": "COD not verified", "id": "COD not verified"},
        {"name": "COD verified", "id": "COD verified"},
        {"name": "Partial Refunded", "id": "Partial Refunded"},
        {"name": "Refunded", "id": "Refunded"},
    ]


def _ensure_invoice_meta_table():
    """Create the invoice-metadata table if it doesn't exist (optiwar2)."""
    with ow_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ops_invoice_meta (
                order_id VARCHAR(64) NOT NULL PRIMARY KEY,
                invoice_generation_date DATETIME NOT NULL,
                generated_by INT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def get_invoice_date(order_id: str):
    """Return the frozen invoice_generation_date for an order, or None."""
    try:
        _ensure_invoice_meta_table()
        with ow_cursor() as cur:
            cur.execute(
                "SELECT invoice_generation_date FROM ops_invoice_meta WHERE order_id=%s",
                (order_id,))
            row = cur.fetchone()
        return row["invoice_generation_date"] if row else None
    except Exception as e:
        log.error("get_invoice_date failed for %s: %s", order_id, e)
        return None


def get_invoice(order_id: str):
    """Return the frozen invoice date and series number, or None."""
    try:
        _ensure_invoice_meta_table()
        with ow_cursor() as cur:
            cur.execute(
                "SELECT invoice_generation_date AS invoice_date, invoice_no "
                "FROM ops_invoice_meta WHERE order_id=%s",
                (order_id,))
            row = cur.fetchone()
        return row if row else None
    except Exception as e:
        log.error("get_invoice failed for %s: %s", order_id, e)
        return None


def issue_invoice_date(order_id: str, dt, changed_by=None) -> bool:
    """Store the invoice date and series number unless an invoice already exists."""
    _ensure_invoice_meta_table()
    invoice_no = invoice_series.allot_and_issue(
        get_ow_connection(), "ops_invoice_series", "ops_invoice_meta",
        """
            INSERT INTO ops_invoice_meta
                (order_id, invoice_generation_date, generated_by, invoice_no)
            VALUES (%s, %s, %s, %s)
        """,
        (order_id, dt, changed_by), order_id, "OW",
        invoice_series.fy_label(dt))
    return invoice_no is not None


def add_history(order_id: str, content: str):
    """Append a single order_history row (no status change)."""
    try:
        with ow_cursor() as cur:
            cur.execute(
                "INSERT INTO order_history (order_history_content, order_id) VALUES (%s, %s)",
                ((content or "").strip(), order_id))
        return True
    except Exception as e:
        log.error("Failed to add order_history for Optiwar order %s: %s", order_id, e)
        return False


def _rx_history_eye(raw):
    formatted = ow_rx.format_eye(raw)
    if formatted is None:
        return str(raw or "—")
    return " ".join(
        f"{label} {formatted[key]}"
        for label, key in (("SPH", "sph"), ("CYL", "cyl"),
                           ("AXIS", "axis"), ("ADD", "add")))


def update_rx_powers(order: dict, rx_id, new_right: str, new_left: str,
                     reference: str, changed_by=None,
                     changed_by_name: str = "") -> dict:
    """Update an order's eye powers and append an Ops-owned audit record."""
    order_id = order.get("order_id", "")
    matching = [
        product for product in order.get("products", [])
        if product.get("rx_id") is not None
        and str(product.get("rx_id")) == str(rx_id)
    ]
    if not matching:
        raise ValueError("Prescription ID is not attached to this order")

    rx = matching[0].get("rx") or {}
    old_right = str(rx.get("right_eye") or "")
    old_left = str(rx.get("left_eye") or "")
    recommendations = rx.get("recommendations")
    original_text = f"RX_POWERS ref: {reference}"[:255]
    changed_from = f"R {old_right} | L {old_left}"[:255]
    changed_to = f"R {new_right} | L {new_left}"[:255]

    with ow_cursor() as cur:
        cur.execute(
            "UPDATE rx_collector SET right_eye = %s, left_eye = %s, "
            "recommendations_updated_at = NOW() WHERE rx_id = %s",
            (new_right, new_left, rx_id))
    with ow_ops_cursor() as cur:
        cur.execute("""
            INSERT INTO ops_rx_change_log
                (ow_order_id, rx_id, original_text, changed_from, changed_to,
                 approved, new_recommendations, changed_by)
            VALUES (%s, %s, %s, %s, %s, 'Yes', %s, %s)
        """, (order_id, rx_id, original_text, changed_from, changed_to,
              recommendations, changed_by))

    name = (changed_by_name or str(changed_by or "Ops")).strip()
    history = (
        f"Prescription changed by Ops ({name}) at the customer's request, "
        f"ref {reference}. Right: {_rx_history_eye(old_right)} → "
        f"{_rx_history_eye(new_right)}; Left: {_rx_history_eye(old_left)} → "
        f"{_rx_history_eye(new_left)}.")
    add_history(order_id, history)
    return {
        "rx_id": rx_id,
        "old": {"right_eye": old_right, "left_eye": old_left},
        "new": {"right_eye": new_right, "left_eye": new_left},
        "recommendations": recommendations,
    }


def last_rx_change(rx_ids) -> dict:
    """Return the latest RX_POWERS audit entry for each requested RX ID."""
    ids = list(dict.fromkeys(rx_ids or []))
    if not ids:
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    try:
        with ow_cursor() as cur:
            cur.execute(
                "SELECT rx_id, changed_at, changed_by, original_text "
                "FROM ops_rx_change_log "
                f"WHERE rx_id IN ({placeholders}) "
                "AND original_text LIKE %s "
                "ORDER BY changed_at DESC, id DESC",
                tuple(ids) + ("RX_POWERS%",))
            rows = cur.fetchall() or []
    except Exception as e:
        log.warning("Could not load Optiwar RX change provenance: %s", e)
        return {}

    latest = {}
    for row in rows:
        rx_id = row.get("rx_id")
        if rx_id in latest:
            continue
        original = str(row.get("original_text") or "")
        reference = original[len("RX_POWERS ref: "):] if original.startswith(
            "RX_POWERS ref: ") else ""
        latest[rx_id] = {
            "changed_at": row.get("changed_at"),
            "changed_by": row.get("changed_by"),
            "reference": reference,
        }
    return latest


def log_partial_refund(order_id: str, amount: str = "", reason: str = "",
                       mode: str = "manual", changed_by=None):
    """Record a partial refund in order_history ONLY — order_status is left
    untouched (stays Shipped). No money is moved from the platform; this is a
    record + notification of a refund handled outside the site."""
    amt = (amount or "").strip()
    why = (reason or "").strip()
    detail = f"Partial refund ({mode})" + (f" {amt}" if amt else "") + " issued"
    if why:
        detail += f" — {why}"
    return add_history(order_id, detail)


def _ensure_upgrade_table():
    """Create/upgrade the free-lens-upgrade metadata table (optiwar2, ops-owned).
    Read-only on product lifecycle/stock — this only records that a complimentary
    lens was upgraded, so the invoice + customer email can reflect it on reprints.
    Also carries lifecycle state (status/lock) for lock + downgrade/withdraw."""
    with ow_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_order_upgrades (
                order_id VARCHAR(64) NOT NULL PRIMARY KEY,
                from_lens VARCHAR(255) NOT NULL,
                to_lens VARCHAR(255) NOT NULL,
                note VARCHAR(500) NULL,
                created_by INT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)
        # Add lifecycle columns if this table predates them.
        want = {
            "status": "VARCHAR(20) NOT NULL DEFAULT 'applied'",
            "locked": "TINYINT(1) NOT NULL DEFAULT 0",
            "locked_by": "INT NULL",
            "locked_at": "DATETIME NULL",
            "withdrawn_by": "INT NULL",
            "withdrawn_at": "DATETIME NULL",
            "withdraw_reason": "VARCHAR(255) NULL",
        }
        for col, ddl in want.items():
            cur.execute("SHOW COLUMNS FROM ow_order_upgrades LIKE %s", (col,))
            if not cur.fetchone():
                cur.execute(f"ALTER TABLE ow_order_upgrades ADD COLUMN {col} {ddl}")


def _ensure_upgrade_log_table():
    """Create the free-lens-upgrade audit trail table (optiwar2)."""
    with ow_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_upgrade_log (
                id INT AUTO_INCREMENT PRIMARY KEY,
                order_id VARCHAR(64) NOT NULL,
                action VARCHAR(30) NOT NULL,
                detail VARCHAR(500) NULL,
                actor_id INT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_ow_upgrade_log_order (order_id)
            )
        """)


def log_upgrade_action(order_id: str, action: str, detail: str = "", actor_id=None):
    """Append a single row to the upgrade audit trail (best-effort)."""
    try:
        _ensure_upgrade_log_table()
        with ow_cursor() as cur:
            cur.execute(
                "INSERT INTO ow_upgrade_log (order_id, action, detail, actor_id) "
                "VALUES (%s, %s, %s, %s)",
                (order_id, action[:30], (detail or "")[:500], actor_id))
        return True
    except Exception as e:
        log.warning("log_upgrade_action failed for %s: %s", order_id, e)
        return False


def get_upgrade_log(order_id: str):
    """Return the upgrade audit trail (newest first) for an order."""
    try:
        _ensure_upgrade_log_table()
        with ow_cursor() as cur:
            cur.execute(
                "SELECT action, detail, actor_id, created_at FROM ow_upgrade_log "
                "WHERE order_id=%s ORDER BY id DESC", (order_id,))
            return cur.fetchall()
    except Exception as e:
        log.warning("get_upgrade_log failed for %s: %s", order_id, e)
        return []


def get_upgrade(order_id: str):
    """Return the recorded free-lens upgrade for an order, or None."""
    try:
        _ensure_upgrade_table()
        with ow_cursor() as cur:
            cur.execute(
                "SELECT from_lens, to_lens, note, status, locked, created_by, created_at, "
                "locked_by, locked_at, withdrawn_by, withdrawn_at, withdraw_reason "
                "FROM ow_order_upgrades WHERE order_id=%s", (order_id,))
            return cur.fetchone()
    except Exception as e:
        log.error("get_upgrade failed for %s: %s", order_id, e)
        return None


def set_upgrade(order_id: str, from_lens: str, to_lens: str,
                note: str = "", changed_by=None):
    """Record/replace a free lens upgrade for an order (status=applied, unlocked)
    and append audit history + upgrade-log rows. Locking is a separate step."""
    from_lens = (from_lens or "").strip()
    to_lens = (to_lens or "").strip()
    note = (note or "").strip()
    try:
        _ensure_upgrade_table()
        with ow_cursor() as cur:
            cur.execute("""
                INSERT INTO ow_order_upgrades
                    (order_id, from_lens, to_lens, note, created_by, status, locked)
                VALUES (%s, %s, %s, %s, %s, 'applied', 0)
                ON DUPLICATE KEY UPDATE from_lens=VALUES(from_lens), to_lens=VALUES(to_lens),
                                        note=VALUES(note), created_by=VALUES(created_by),
                                        status='applied'
            """, (order_id, from_lens, to_lens, note or None, changed_by))
        add_history(order_id,
                    f"Free lens upgrade recorded: {from_lens} -> {to_lens} (no charge)"
                    + (f" — {note}" if note else ""))
        log_upgrade_action(order_id, "recorded",
                           f"{from_lens} -> {to_lens}" + (f" — {note}" if note else ""),
                           changed_by)
        log.info("Free lens upgrade set for %s: %s -> %s", order_id, from_lens, to_lens)
        return True
    except Exception as e:
        log.error("set_upgrade failed for %s: %s", order_id, e)
        return False


def lock_upgrade(order_id: str, actor_id=None):
    """Lock a recorded upgrade so the invoice/detail are finalized and read-only."""
    try:
        _ensure_upgrade_table()
        with ow_cursor() as cur:
            cur.execute(
                "UPDATE ow_order_upgrades SET locked=1, locked_by=%s, locked_at=NOW() "
                "WHERE order_id=%s AND status='applied'", (actor_id, order_id))
        add_history(order_id, "Free lens upgrade locked (invoice finalized)")
        log_upgrade_action(order_id, "locked", "Upgrade locked; invoice finalized", actor_id)
        return True
    except Exception as e:
        log.error("lock_upgrade failed for %s: %s", order_id, e)
        return False


def unlock_upgrade(order_id: str, actor_id=None):
    """Unlock a locked upgrade so it can be edited/corrected, then re-locked."""
    try:
        _ensure_upgrade_table()
        with ow_cursor() as cur:
            cur.execute(
                "UPDATE ow_order_upgrades SET locked=0, locked_by=NULL, locked_at=NULL "
                "WHERE order_id=%s", (order_id,))
        add_history(order_id, "Free lens upgrade unlocked for editing")
        log_upgrade_action(order_id, "unlocked", "Upgrade unlocked for editing", actor_id)
        return True
    except Exception as e:
        log.error("unlock_upgrade failed for %s: %s", order_id, e)
        return False


def withdraw_upgrade(order_id: str, reason: str = "", actor_id=None):
    """Downgrade/withdraw a free upgrade (customer refused/withdrawn). Reverts the
    order to its original complimentary lens on the invoice (status=withdrawn) and
    logs it. Does NOT touch rx_collector or the customer's purchase."""
    reason = (reason or "").strip()
    try:
        _ensure_upgrade_table()
        with ow_cursor() as cur:
            cur.execute(
                "UPDATE ow_order_upgrades SET status='withdrawn', locked=0, "
                "withdrawn_by=%s, withdrawn_at=NOW(), withdraw_reason=%s "
                "WHERE order_id=%s", (actor_id, reason or None, order_id))
        add_history(order_id,
                    "Free lens upgrade WITHDRAWN — reverted to original complimentary lens"
                    + (f" — {reason}" if reason else ""))
        log_upgrade_action(order_id, "withdrawn",
                           "Reverted to original complimentary lens"
                           + (f" — {reason}" if reason else ""), actor_id)
        return True
    except Exception as e:
        log.error("withdraw_upgrade failed for %s: %s", order_id, e)
        return False


def update_status(order_id: str, status_name: str, comment: str = "", changed_by=None):
    """Manually update an optiwar order's status + append an order_history entry."""
    try:
        content = (comment or "").strip() or f"Status changed to {status_name}"
        with ow_cursor() as cur:
            cur.execute(
                "INSERT INTO order_status (order_status_name, order_id) VALUES (%s, %s)",
                (status_name, order_id))
            cur.execute(
                "INSERT INTO order_history (order_history_content, order_id) VALUES (%s, %s)",
                (content, order_id))
        log.info("Optiwar order %s status -> %s", order_id, status_name)
        return True
    except Exception as e:
        log.error("Failed to update Optiwar order %s status: %s", order_id, e)
        return False


def mark_shipped(order_id: str, awb: str, courier: str = "Delhivery", changed_by=None):
    """Write 'Shipped' back to optiwar2: add an order_status row + an order_history entry."""
    try:
        with ow_cursor() as cur:
            cur.execute(
                "INSERT INTO order_status (order_status_name, order_id) VALUES ('Shipped', %s)",
                (order_id,))
            cur.execute(
                "INSERT INTO order_history (order_history_content, order_id) VALUES (%s, %s)",
                (f"Shipped via {courier}. AWB: {awb}", order_id))
        log.info("Optiwar order %s marked Shipped (AWB %s)", order_id, awb)
        return True
    except Exception as e:
        log.error("Failed to mark Optiwar order %s shipped: %s", order_id, e)
        return False


def _ensure_rx_audit_columns():
    """Add a nullable audit timestamp column to optiwar2 rx_collector if missing.
    Also ensure a local audit-log table exists in the platform DB."""
    try:
        with ow_cursor() as cur:
            cur.execute("SHOW COLUMNS FROM rx_collector LIKE 'recommendations_updated_at'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE rx_collector ADD COLUMN recommendations_updated_at datetime NULL DEFAULT NULL")
                log.info("rx_collector: added recommendations_updated_at column")
    except Exception as e:
        log.warning("Could not ensure rx_collector audit column: %s", e)


def apply_rx_changes(order: dict, changes: list, changed_by: int = None) -> list:
    """Persist approved order changes back into optiwar2 rx_collector.recommendations,
    stamp an update timestamp for audit, and log each change to the platform DB.
    Only applies changes marked approved == 'Yes'. Returns the list of applied changes."""
    if not changes:
        return []
    _ensure_rx_audit_columns()

    applied = []
    order_id = order.get("order_id", "")
    approved_changes = [c for c in changes
                        if (c.get("approved") or "").strip().lower() == "yes"
                        and c.get("item") and c.get("changed_to")]
    if not approved_changes:
        return []

    seen_rx = set()
    for p in order.get("products", []):
        rx_id = p.get("rx_id")
        rx = p.get("rx") or {}
        rec = rx.get("recommendations")
        if not rx_id or rx_id in seen_rx or not rec:
            continue
        seen_rx.add(rx_id)

        new_rec = rec
        row_applied = []
        for c in approved_changes:
            item = c["item"].strip()
            to = c["changed_to"].strip()
            lower = new_rec.lower()
            idx = lower.find(item.lower())
            if idx >= 0:
                new_rec = new_rec[:idx] + to + new_rec[idx + len(item):]
                row_applied.append((item, to))

        # Also sync any add-on name fields that match a changed item, so line-item
        # views (e.g. the GST tax invoice) reflect the approved change too.
        addon_updates = {}
        for (frm, to) in row_applied:
            for i in (1, 2, 3):
                if (rx.get(f"addon_{i}_name") or "").strip().lower() == frm.strip().lower():
                    addon_updates[f"addon_{i}_name"] = to

        if new_rec != rec:
            try:
                set_sql = "recommendations = %s, recommendations_updated_at = NOW()"
                params = [new_rec]
                for col, val in addon_updates.items():
                    set_sql += f", {col} = %s"
                    params.append(val)
                params.append(rx_id)
                with ow_cursor() as cur:
                    cur.execute(f"UPDATE rx_collector SET {set_sql} WHERE rx_id = %s", params)
                for (frm, to) in row_applied:
                    with ow_ops_cursor() as cur:
                        cur.execute("""
                            INSERT INTO ops_rx_change_log
                                (ow_order_id, rx_id, original_text, changed_from, changed_to,
                                 approved, new_recommendations, changed_by)
                            VALUES (%s, %s, %s, %s, %s, 'Yes', %s, %s)
                        """, (order_id, rx_id, rec, frm, to, new_rec, changed_by))
                    applied.append({"rx_id": rx_id, "from": frm, "to": to})
                log.info("rx_collector %s updated for order %s: %s", rx_id, order_id, row_applied)
            except Exception as e:
                log.error("Failed to update rx_collector %s for %s: %s", rx_id, order_id, e)
    return applied
