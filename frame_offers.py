"""One-time frame replacement offers for QC failures and stock-outs."""

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from . import brand_compliance
from . import face_scan_invites as fsi
from . import brand_compliance


_HELD_IDS_SQL = ",".join(str(product_id) for product_id in brand_compliance.HELD)
_HELD_FILTER_SQL = (" AND p.product_id NOT IN (%s)" % _HELD_IDS_SQL
                    if _HELD_IDS_SQL else "")


SCHEMA = """
CREATE TABLE IF NOT EXISTS frame_swap_offers (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    offer_uuid CHAR(36) NOT NULL,
    order_id VARCHAR(255) NOT NULL,
    order_line_id BIGINT NOT NULL,
    reason VARCHAR(16) NOT NULL,
    failed_product_id BIGINT NOT NULL,
    failed_price_minor BIGINT NOT NULL,
    candidates_json MEDIUMTEXT NOT NULL,
    token_hash CHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'OFFERED',
    chosen_product_id BIGINT NULL,
    price_diff_minor BIGINT NULL,
    settlement VARCHAR(16) NULL,
    created_by VARCHAR(191) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL,
    opened_at DATETIME NULL,
    decided_at DATETIME NULL,
    reminder_sent_at DATETIME NULL,
    alerted_at DATETIME NULL,
    settled_at DATETIME NULL,
    settled_ref VARCHAR(191) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_frame_offer_uuid (offer_uuid),
    UNIQUE KEY uq_frame_offer_token (token_hash),
    KEY idx_frame_offer_order_line (order_line_id, created_at),
    KEY idx_frame_offer_status (status, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

QUERY = f"""SELECT p.product_id, p.product_code, p.product_name, p.product_size, p.color_display,
       p.product_special_price, p.product_quantity,
       (p.product_category_clubmaster*f.product_category_clubmaster + p.product_category_browline*f.product_category_browline
      + p.product_category_rectangle*f.product_category_rectangle + p.product_category_square*f.product_category_square
      + p.product_category_round*f.product_category_round + p.product_category_oval*f.product_category_oval
      + p.product_category_wayfarer*f.product_category_wayfarer + p.product_category_aviator*f.product_category_aviator
      + p.product_category_cateye*f.product_category_cateye) AS shape_match
FROM products p JOIN products f ON f.product_id = 667
WHERE p.product_category = f.product_category
  AND p.product_vertical = f.product_vertical
  AND p.product_status = 'ACTIVE' AND p.product_quantity > 0
  AND p.sell_on_in = 1
  {_HELD_FILTER_SQL}
  AND COALESCE(p.discontinued, 0) = 0
  AND p.product_id <> f.product_id
  AND p.product_diameter BETWEEN f.product_diameter - 4 AND f.product_diameter + 4
  AND p.product_bridge BETWEEN f.product_bridge - 3 AND f.product_bridge + 3
  AND p.product_special_price <= f.product_special_price + 300
ORDER BY shape_match DESC,
         ABS(p.product_diameter - f.product_diameter) + ABS(p.product_bridge - f.product_bridge)
           + ABS(p.product_lenght - f.product_lenght) / 5,
         (p.color_filter <=> f.color_filter) DESC,
         ABS(p.product_special_price - f.product_special_price)
LIMIT 8;"""


class OfferError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def ensure_schema(db):
    db.cursor().execute(SCHEMA)


def _minor(value):
    try:
        return int((Decimal(str(value or 0)) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError, TypeError):
        return 0


def settlement_for(price_diff_minor):
    diff = int(price_diff_minor)
    if abs(diff) < 5000:
        return "WAIVED"
    if diff <= -5000:
        return "REFUND_DUE"
    return "PAYMENT_DUE"


def price_label(candidate_price_minor, failed_price_minor, qty=1):
    line_diff = (int(candidate_price_minor) - int(failed_price_minor)) * int(qty or 1)
    if settlement_for(line_diff) in ("WAIVED", "REFUND_DUE"):
        return "No extra charge"
    return "Pay ₹%s more" % (line_diff // 100)


def send_gate(reason, wa_template):
    if str(reason or "").upper() == "OUT_OF_STOCK":
        return False, "Needs its own approved wording/template"
    if not str(wa_template or "").strip():
        return False, "Waiting for msg91 approval of ow_frame_qc_replace"
    return True, ""


def _json_candidates(value):
    try:
        rows = json.loads(value or "[]")
    except (TypeError, ValueError):
        rows = []
    return rows if isinstance(rows, list) else []


def match_sql(site_from):
    column = "sell_on_in" if (site_from or "").lower() == "in.optiwar.com" else "sell_on_com"
    return (QUERY
            .replace("f.product_id = 667", "f.product_id = %s")
            .replace("p.product_quantity > 0", "p.product_quantity >= %s")
            .replace("p.sell_on_in = 1", "p.%s = 1" % column)
            # never offer a frame held off sale for a trademark reason
            .replace("AND p.product_id <> f.product_id",
                     "AND p.product_id <> f.product_id" + brand_compliance.sql_exclude("p")))


def matches(cursor, product_id, site_from, qty=1):
    cursor.execute(match_sql(site_from), (int(product_id), int(qty or 1)))
    return cursor.fetchall()


def _site_host(site_from):
    return "in.optiwar.com" if (site_from or "").lower() == "in.optiwar.com" else "optiwar.com"


def token_for(offer_uuid, secret):
    key = secret if isinstance(secret, bytes) else str(secret or "").encode()
    if not key:
        raise OfferError("token_key_unavailable", "Frame offer token key is unavailable", 503)
    digest = hmac.new(key, ("frame-offer:" + str(offer_uuid)).encode(),
                      hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def link_for(row, secret, now=None):
    if row.get("status") != "OFFERED":
        raise OfferError("offer_closed", "Frame offer is no longer available", 404)
    expires_at = row.get("expires_at")
    if not expires_at or expires_at <= (now or datetime.now()):
        raise OfferError("offer_expired", "Frame offer has expired", 410)
    token = token_for(row["offer_uuid"], secret)
    if fsi.hash_token(token) != row.get("token_hash"):
        raise OfferError("token_key_changed", "Frame offer token key has changed", 503)
    return "https://%s/fq/%s" % (_site_host(row.get("site_from")), token)


def _candidate_rows(cursor, rows):
    ids = [int(row["product_id"]) for row in rows]
    images = {}
    if ids:
        cursor.execute(
            "SELECT product_id, product_image FROM products WHERE product_id IN (%s)"
            % ", ".join(["%s"] * len(ids)), tuple(ids))
        for row in cursor.fetchall():
            images[int(row["product_id"])] = row.get("product_image") or ""
    result = []
    for row in rows:
        pid = int(row["product_id"])
        paths = [part.strip().lstrip("./") for part in images.get(pid, "").split(",")
                 if part.strip()]
        result.append({
            "product_id": pid,
            "code": row.get("product_code") or "",
            "name": row.get("product_name") or "",
            "size": row.get("product_size") or "",
            "colour": row.get("color_display") or "",
            "price_minor": _minor(row.get("product_special_price")),
            "image_url": "https://optiwar.com/static/" + paths[0] if paths else "",
        })
    return result


def _line(cursor, order_id, order_line_id, lock=False):
    cursor.execute(
        """SELECT o.order_line_id, o.order_id, o.product_id, o.order_quantity,
                  o.order_total, o.site_from, c.customer_id, c.customer_name,
                  c.customer_email, c.customer_phone, p.product_code, p.product_name,
                  p.product_special_price, p.product_image, p.product_size,
                  p.color_display
           FROM orders o
           LEFT JOIN customers c ON c.customer_id=o.customer_id
           JOIN products p ON p.product_id=o.product_id
           WHERE o.order_id=%s AND o.order_line_id=%s""" +
        (" FOR UPDATE" if lock else ""),
        (str(order_id), int(order_line_id)))
    return cursor.fetchone()


def _history(cursor, order_id, site_from, text):
    cursor.execute(
        "INSERT INTO order_history (order_history_content, order_id, site_from) "
        "VALUES (%s, %s, %s)", (text, str(order_id), site_from))


def refund_balance(cursor, order_id):
    from . import paid_orders as paid_order_service
    from . import refunds as refund_service
    refund_rows = refund_service.ledger_refunds(cursor, str(order_id))
    paid_total = paid_order_service.order_amount_minor(cursor, str(order_id))
    return paid_total, refund_service.committed_minor(refund_rows)


def remaining_refundable_minor(cursor, order_id):
    paid_total, committed = refund_balance(cursor, order_id)
    return max(0, paid_total - committed)


def create_offer(db, order_id, order_line_id, reason, product_ids, created_by,
                 now=None, token_secret=None):
    reason = str(reason or "").upper()
    if reason not in ("QC_FAILED", "OUT_OF_STOCK"):
        raise OfferError("invalid_reason", "Choose QC_FAILED or OUT_OF_STOCK")
    try:
        ids = list(dict.fromkeys(int(pid) for pid in (product_ids or [])))
    except (TypeError, ValueError):
        raise OfferError("invalid_candidates", "Choose between one and six frames")
    if not 1 <= len(ids) <= 6:
        raise OfferError("invalid_candidates", "Choose between one and six frames")
    now = now or datetime.now()
    try:
        order_line_id = int(order_line_id)
    except (TypeError, ValueError):
        raise OfferError("invalid_line", "Order line is required")
    actor = str(created_by or "ops")
    ensure_schema(db)
    cur = db.cursor()
    try:
        line = _line(cur, order_id, order_line_id, lock=True)
        if not line:
            raise OfferError("line_not_found", "Order line not found", 404)
        qty = int(line.get("order_quantity") or 1)
        candidates = _candidate_rows(
            cur, [row for row in matches(
                cur, line["product_id"], line.get("site_from"), qty)
                  if int(row["product_id"]) in ids])
        if len(candidates) != len(ids):
            raise OfferError("candidate_changed",
                             "A selected frame is no longer an available match", 409)
        cur.execute(
            "UPDATE frame_swap_offers SET status='CANCELLED' "
            "WHERE order_line_id=%s AND status='OFFERED'", (int(order_line_id),))
        offer_uuid = str(uuid.uuid4())
        token = token_for(offer_uuid, token_secret)
        expires_at = now + timedelta(days=7)
        cur.execute(
            """INSERT INTO frame_swap_offers
               (offer_uuid, order_id, order_line_id, reason, failed_product_id,
                failed_price_minor, candidates_json, token_hash, status, created_by,
                created_at, expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'OFFERED',%s,%s,%s)""",
            (offer_uuid, str(order_id), int(order_line_id), reason,
             int(line["product_id"]), _minor(line.get("product_special_price")),
             json.dumps(candidates, ensure_ascii=False), fsi.hash_token(token),
             actor[:191], now, expires_at))
        if reason == "QC_FAILED":
            cur.execute(
                "INSERT IGNORE INTO stock_restocks "
                "(order_line_id, order_id, product_id, qty, refund_id, reason, restocked_by) "
                "VALUES (%s,%s,%s,0,NULL,'qc_failed',%s)",
                (int(order_line_id), str(order_id), int(line["product_id"]),
                 actor[:191]))
        codes = ", ".join(c["code"] for c in candidates)
        if reason == "QC_FAILED":
            history = ("Frame %s failed quality check; replacement offered: %s (by %s)"
                       % (line.get("product_code") or "", codes, actor))
        else:
            history = ("Frame %s out of stock; replacement offered: %s (by %s)"
                       % (line.get("product_code") or "", codes, actor))
        _history(cur, order_id, line.get("site_from"), history)
        db.commit()
    except Exception:
        db.rollback()
        raise
    row = get_by_uuid(db, offer_uuid)
    row["site_from"] = line.get("site_from")
    row["order_quantity"] = qty
    return row, token, "https://%s/fq/%s" % (_site_host(line.get("site_from")), token)


def get_by_uuid(db, offer_uuid, lock=False):
    cur = db.cursor()
    cur.execute("SELECT * FROM frame_swap_offers WHERE offer_uuid=%s" +
                (" FOR UPDATE" if lock else ""), (str(offer_uuid),))
    row = cur.fetchone()
    if row:
        row["candidates"] = _json_candidates(row.get("candidates_json"))
    return row


def by_token(db, token, lock=False):
    if not token or len(token) > 128:
        return None
    cur = db.cursor()
    cur.execute("SELECT * FROM frame_swap_offers WHERE token_hash=%s" +
                (" FOR UPDATE" if lock else ""), (fsi.hash_token(token),))
    row = cur.fetchone()
    if row:
        row["candidates"] = _json_candidates(row.get("candidates_json"))
    return row


def public_view(row):
    if not row:
        return None
    return {key: row.get(key) for key in (
        "offer_uuid", "order_id", "order_line_id", "reason", "status",
        "failed_product_id", "failed_price_minor", "candidates",
        "chosen_product_id", "price_diff_minor", "settlement", "created_by",
        "created_at", "expires_at", "opened_at", "decided_at",
        "reminder_sent_at", "alerted_at", "settled_at", "settled_ref")}


def latest_for_order(db, order_id, order_line_id=None):
    cur = db.cursor()
    query = "SELECT * FROM frame_swap_offers WHERE order_id=%s"
    args = [str(order_id)]
    if order_line_id is not None:
        query += " AND order_line_id=%s"
        args.append(int(order_line_id))
    query += " ORDER BY created_at DESC, id DESC"
    cur.execute(query, tuple(args))
    rows = cur.fetchall()
    latest = {}
    for row in rows:
        line_id = int(row["order_line_id"])
        if line_id not in latest:
            row["candidates"] = _json_candidates(row.get("candidates_json"))
            latest[line_id] = row
    return list(latest.values())


def mark_opened(db, row, now=None):
    cur = db.cursor()
    cur.execute("UPDATE frame_swap_offers SET opened_at=COALESCE(opened_at,%s) "
                "WHERE id=%s AND status='OFFERED'", (now or datetime.now(), row["id"]))
    db.commit()
    return get_by_uuid(db, row["offer_uuid"])


def decide(db, row, product_id=None, refund=False, now=None):
    now = now or datetime.now()
    db.begin()
    cur = db.cursor()
    try:
        cur.execute("SELECT * FROM frame_swap_offers WHERE id=%s FOR UPDATE",
                    (int(row["id"]),))
        live = cur.fetchone()
        if not live or live.get("status") != "OFFERED":
            raise OfferError("used", "This offer is no longer available", 410)
        if live["expires_at"] <= now:
            cur.execute("UPDATE frame_swap_offers SET status='EXPIRED' WHERE id=%s",
                        (int(live["id"]),))
            db.commit()
            raise OfferError("expired", "This offer has expired", 410)
        line = _line(cur, live["order_id"], live["order_line_id"], lock=True)
        if not line:
            raise OfferError("line_not_found", "Order line not found", 404)
        from . import refunds as refund_service
        paid_total_minor, refunded_minor = refund_balance(
            cur, str(live["order_id"]))
        cur.execute(
            "SELECT order_status_name FROM order_status WHERE order_id=%s "
            "ORDER BY order_status_id DESC LIMIT 1", (str(live["order_id"]),))
        status_row = cur.fetchone() or {}
        if (status_row.get("order_status_name") == refund_service.STATUS_FULL
                or (paid_total_minor > 0 and refunded_minor >= paid_total_minor)):
            cur.execute("UPDATE frame_swap_offers SET status='CANCELLED' WHERE id=%s",
                        (int(live["id"]),))
            db.commit()
            raise OfferError("closed", "This order has already been refunded", 410)
        candidates = _json_candidates(live.get("candidates_json"))
        if refund:
            failed = line.get("product_code") or ""
            cur.execute("UPDATE frame_swap_offers SET status='REFUND_REQUESTED', "
                        "decided_at=%s WHERE id=%s", (now, int(live["id"])))
            _history(cur, live["order_id"], line.get("site_from"),
                     "Customer asked for a full refund via EWS (frame %s failed QC)" % failed)
            result = {"action": "refund", "order_id": live["order_id"],
                      "product_code": failed,
                      "amount_minor": max(0, paid_total_minor - refunded_minor)}
        else:
            qty = int(line.get("order_quantity") or 1)
            try:
                pid = int(product_id)
            except (TypeError, ValueError):
                raise OfferError("invalid_candidate", "Choose one of the offered frames")
            candidate = next((c for c in candidates if int(c["product_id"]) == pid), None)
            if not candidate:
                raise OfferError("invalid_candidate", "Choose one of the offered frames")
            cur.execute(
                "UPDATE products SET product_quantity=product_quantity-%s "
                "WHERE product_id=%s AND product_quantity>=%s AND product_status='ACTIVE'",
                (qty, pid, qty))
            if cur.rowcount != 1:
                raise OfferError("sold_out", "That frame just sold out. Please choose another.", 409)
            cur.execute("SELECT product_quantity FROM products WHERE product_id=%s", (pid,))
            stock = cur.fetchone()
            if stock and int(stock["product_quantity"]) <= 0:
                from .paid_orders import mark_out_of_stock
                mark_out_of_stock(cur, pid)
            cur.execute("UPDATE orders SET product_id=%s WHERE order_line_id=%s AND order_id=%s",
                        (pid, int(live["order_line_id"]), live["order_id"]))
            if cur.rowcount != 1:
                raise OfferError("line_changed", "Order line could not be updated", 409)
            cur.execute("DELETE FROM stock_restocks WHERE order_line_id=%s AND reason='qc_failed'",
                        (int(live["order_line_id"]),))
            diff = (int(candidate["price_minor"]) - int(live["failed_price_minor"])) * qty
            settlement = settlement_for(diff)
            status = "PAYMENT_PENDING" if settlement == "PAYMENT_DUE" else "SWAPPED"
            cur.execute(
                "UPDATE frame_swap_offers SET status=%s, chosen_product_id=%s, "
                "price_diff_minor=%s, settlement=%s, decided_at=%s WHERE id=%s",
                (status, pid, diff, settlement, now, int(live["id"])))
            amount = abs(diff)
            _history(cur, live["order_id"], line.get("site_from"),
                     "Customer chose replacement frame %s via EWS; difference ₹%s (%s)"
                     % (candidate["code"], format(Decimal(abs(diff)) / 100, "f"), settlement))
            result = {"action": "frame", "order_id": live["order_id"],
                      "product_code": candidate["code"], "amount_minor": amount,
                      "settlement": settlement, "diff_minor": diff}
        db.commit()
        return result
    except OfferError as exc:
        if exc.code not in ("expired", "closed"):
            db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def mark_settled(db, offer_uuid, ref, now=None):
    cur = db.cursor()
    cur.execute(
        "UPDATE frame_swap_offers SET settled_at=%s, settled_ref=%s "
        "WHERE offer_uuid=%s AND status IN ('SWAPPED','PAYMENT_PENDING','REFUND_REQUESTED')",
        (now or datetime.now(), str(ref or "")[:191] or None, str(offer_uuid)))
    db.commit()
    return cur.rowcount == 1


def open_offers(db):
    cur = db.cursor()
    cur.execute("SELECT f.offer_uuid, f.order_id, f.order_line_id, f.reason, f.status, "
                "f.failed_product_id, f.failed_price_minor, f.candidates_json, "
                "f.created_by, f.created_at, f.expires_at, f.opened_at, "
                "f.reminder_sent_at, f.alerted_at, o.site_from, o.order_quantity "
                "FROM frame_swap_offers f JOIN orders o ON o.order_line_id=f.order_line_id "
                "WHERE f.status='OFFERED' ORDER BY f.created_at")
    rows = cur.fetchall()
    for row in rows:
        row["candidates"] = _json_candidates(row.get("candidates_json"))
    return rows


def mark(db, offer_uuid, action, now=None):
    field = {"reminder_sent": "reminder_sent_at",
             "alerted": "alerted_at",
             "expired": "status"} .get(action)
    if not field:
        raise OfferError("invalid_action", "Unknown frame offer action")
    cur = db.cursor()
    if action == "expired":
        cur.execute("UPDATE frame_swap_offers SET status='EXPIRED' "
                    "WHERE offer_uuid=%s AND status='OFFERED'", (str(offer_uuid),))
    else:
        cur.execute("UPDATE frame_swap_offers SET %s=COALESCE(%s,%s) "
                    "WHERE offer_uuid=%%s AND status='OFFERED'" %
                    (field, field, "%s"), (now or datetime.now(), str(offer_uuid)))
    db.commit()
    return cur.rowcount == 1
