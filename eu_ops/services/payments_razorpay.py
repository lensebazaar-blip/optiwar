"""Razorpay payment links for ops-created Optiwar orders.

Links are created on demand by an operator; the paid callback is received here,
signature-verified and recorded in the ops database (oslb6). Nothing is written
to optiwar2 — the payment row for Optiwar is emitted as SQL for manual execution,
same rule as order creation.
"""
import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import config
from database import db_cursor

log = logging.getLogger("payments_razorpay")

API_BASE = "https://api.razorpay.com/v1"
# INR and EUR are both minor-unit currencies (paise / cents)
MINOR_UNITS = 100


def _auth_header():
    raw = f"{config.RAZORPAY_KEY_ID}:{config.RAZORPAY_KEY_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def configured() -> bool:
    return bool(config.RAZORPAY_KEY_ID and config.RAZORPAY_KEY_SECRET)


def _call(path: str, payload: dict = None, method: str = "GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(f"{API_BASE}{path}", data=data, method=method)
    req.add_header("Authorization", _auth_header())
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=40) as resp:
            return True, json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            err = json.loads(body)["error"]["description"]
        except Exception:
            err = body[:300]
        log.error("razorpay %s %s failed: %s %s", method, path, e.code, err)
        return False, {"error": f"{e.code}: {err}"}
    except URLError as e:
        log.error("razorpay %s %s unreachable: %s", method, path, e)
        return False, {"error": str(e)}


# ─── Ops-side ledger ─────────────────────────────────────────────────

def _ensure_table():
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_payment_links (
                id            int AUTO_INCREMENT PRIMARY KEY,
                order_id      varchar(32)  NOT NULL,
                draft_id      int          NULL,
                site          varchar(32)  NOT NULL,
                currency      char(3)      NOT NULL,
                amount_minor  int          NOT NULL,
                link_id       varchar(64)  NOT NULL,
                short_url     varchar(255) NOT NULL,
                status        varchar(24)  NOT NULL DEFAULT 'created',
                payment_id    varchar(64)  NULL,
                paid_at       datetime     NULL,
                created_by    varchar(64)  NULL,
                created_at    datetime     NOT NULL DEFAULT current_timestamp(),
                notified_at   datetime     NULL,
                UNIQUE KEY uq_link (link_id),
                KEY idx_order (order_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute("ALTER TABLE ow_payment_links "
                    "ADD COLUMN IF NOT EXISTS notified_at datetime NULL")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_payment_events (
                id           int AUTO_INCREMENT PRIMARY KEY,
                event        varchar(64)  NOT NULL,
                order_id     varchar(32)  NULL,
                link_id      varchar(64)  NULL,
                payment_id   varchar(64)  NULL,
                amount_minor int          NULL,
                currency     char(3)      NULL,
                verified     tinyint(1)   NOT NULL DEFAULT 0,
                applied_sql  tinyint(1)   NOT NULL DEFAULT 0,
                raw          longtext     NULL,
                created_at   datetime     NOT NULL DEFAULT current_timestamp(),
                UNIQUE KEY uq_event (event, payment_id, link_id),
                KEY idx_order (order_id),
                KEY idx_payment (payment_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")


def links_for_order(order_id: str):
    _ensure_table()
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_payment_links WHERE order_id = %s "
                    "ORDER BY id DESC", (order_id,))
        return cur.fetchall()


def claim_link_notify(link_id: str) -> bool:
    """Take the right to send this link to the customer, once.

    The UPDATE is the lock: a double-clicked Create, or a click on a link that
    already exists, cannot message the customer twice."""
    _ensure_table()
    with db_cursor() as cur:
        cur.execute("UPDATE ow_payment_links SET notified_at = NOW() "
                    "WHERE link_id = %s AND notified_at IS NULL", (link_id,))
        return cur.rowcount == 1


def release_link_notify(link_id: str):
    """Hand the claim back when nothing was actually delivered."""
    with db_cursor() as cur:
        cur.execute("UPDATE ow_payment_links SET notified_at = NULL "
                    "WHERE link_id = %s", (link_id,))


def events_for_order(order_id: str):
    _ensure_table()
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_payment_events WHERE order_id = %s "
                    "ORDER BY id DESC", (order_id,))
        return cur.fetchall()


# ─── Link creation ───────────────────────────────────────────────────

def create_link(order_id: str, amount: int, currency: str, site: str,
                name: str = "", email: str = "", phone: str = "",
                description: str = "", draft_id: int = None,
                created_by: str = "ops", expire_days: int = 7):
    """Create a Razorpay payment link for a whole-unit amount (INR or EUR).

    Reuses an existing unpaid link for the same order and amount rather than
    creating duplicates the customer could pay twice."""
    if not configured():
        return False, "Razorpay keys are not configured"
    if amount <= 0:
        return False, "Amount must be greater than zero"

    amount_minor = int(amount) * MINOR_UNITS
    for row in links_for_order(order_id):
        if (row["status"] in ("created", "partially_paid")
                and row["amount_minor"] == amount_minor
                and row["currency"] == currency):
            return True, row

    customer = {}
    if name:
        customer["name"] = name
    if email:
        customer["email"] = email
    if phone:
        customer["contact"] = phone if phone.startswith("+") else f"+91{phone}"

    payload = {
        "amount": amount_minor,
        "currency": currency,
        "accept_partial": False,
        "description": description or f"Optiwar order {order_id}",
        "reference_id": order_id,
        "customer": customer,
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "callback_url": f"https://{site}/success/{order_id}",
        "callback_method": "get",
        "notes": {"order_id": order_id, "site": site, "source": "ops"},
    }
    if expire_days:
        payload["expire_by"] = int(time.time()) + expire_days * 86400
    ok, resp = _call("/payment_links", payload, method="POST")
    if not ok:
        return False, resp.get("error", "Razorpay rejected the request")

    _ensure_table()
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO ow_payment_links
                (order_id, draft_id, site, currency, amount_minor, link_id,
                 short_url, status, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (order_id, draft_id, site, currency, amount_minor,
                     resp["id"], resp["short_url"], resp.get("status", "created"),
                     created_by))
    log.info("razorpay link %s created for order %s (%s %s)",
             resp["id"], order_id, currency, amount)
    return True, {"link_id": resp["id"], "short_url": resp["short_url"],
                  "status": resp.get("status", "created"),
                  "amount_minor": amount_minor, "currency": currency,
                  "order_id": order_id}


def cancel_link(link_id: str):
    ok, resp = _call(f"/payment_links/{link_id}/cancel", {}, method="POST")
    if ok:
        with db_cursor() as cur:
            cur.execute("UPDATE ow_payment_links SET status = 'cancelled' "
                        "WHERE link_id = %s", (link_id,))
    return ok, resp


def refresh_link(link_id: str):
    """Poll Razorpay for a link's current state — the safety net for webhooks
    that never arrived."""
    ok, resp = _call(f"/payment_links/{link_id}")
    if not ok:
        return False, resp
    status = resp.get("status", "")
    payment_id = ""
    for p in resp.get("payments") or []:
        if p.get("status") == "captured":
            payment_id = p.get("payment_id") or ""
            break
    with db_cursor() as cur:
        cur.execute("""UPDATE ow_payment_links
                       SET status = %s,
                           payment_id = COALESCE(NULLIF(%s, ''), payment_id),
                           paid_at = CASE WHEN %s = 'paid' AND paid_at IS NULL
                                          THEN current_timestamp() ELSE paid_at END
                       WHERE link_id = %s""",
                    (status, payment_id, status, link_id))
    return True, {"status": status, "payment_id": payment_id}


# ─── Webhook ─────────────────────────────────────────────────────────

def verify_signature(raw_body: bytes, signature: str) -> bool:
    """Razorpay signs the raw request body with the webhook secret (HMAC-SHA256).

    Without a configured secret this returns False — an unverified webhook must
    never be trusted to mark an order paid."""
    secret = getattr(config, "RAZORPAY_WEBHOOK_SECRET", "") or ""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def record_event(event: str, body: dict, verified: bool):
    """Store the callback. Optiwar's payment_collector row is NOT written here —
    payment_sql() produces it for manual execution.

    Razorpay retries webhooks, so the insert is idempotent on
    (event, payment_id, link_id) and a repeat is recorded once only."""
    payload = body.get("payload") or {}
    link = (payload.get("payment_link") or {}).get("entity") or {}
    payment = (payload.get("payment") or {}).get("entity") or {}
    order_id = (link.get("reference_id")
                or (payment.get("notes") or {}).get("order_id") or "")
    amount = payment.get("amount") or link.get("amount")
    currency = payment.get("currency") or link.get("currency")

    _ensure_table()
    duplicate = False
    with db_cursor() as cur:
        cur.execute("SELECT id FROM ow_payment_events WHERE event = %s "
                    "AND payment_id <=> %s AND link_id <=> %s",
                    (event, payment.get("id"), link.get("id")))
        if cur.fetchone():
            duplicate = True
        else:
            cur.execute("""
                INSERT INTO ow_payment_events
                    (event, order_id, link_id, payment_id, amount_minor, currency,
                     verified, raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        (event, order_id or None, link.get("id"), payment.get("id"),
                         amount, currency, 1 if verified else 0,
                         json.dumps(body)[:60000]))
        if verified and link.get("id"):
            cur.execute("""UPDATE ow_payment_links
                           SET status = %s, payment_id = %s,
                               paid_at = CASE WHEN %s IN ('paid','captured')
                                              AND paid_at IS NULL
                                         THEN current_timestamp() ELSE paid_at END
                           WHERE link_id = %s""",
                        (link.get("status") or event, payment.get("id"),
                         link.get("status") or "", link.get("id")))
    return {"order_id": order_id, "payment_id": payment.get("id"),
            "link_id": link.get("id"), "amount_minor": amount,
            "currency": currency, "duplicate": duplicate}


def amount_matches(link_id: str, amount_minor, currency: str) -> bool:
    """A paid callback is only trusted if it settles the amount and currency we
    asked for — a tampered or mismatched notification must not produce SQL."""
    if not link_id or amount_minor is None:
        return False
    with db_cursor() as cur:
        cur.execute("SELECT amount_minor, currency FROM ow_payment_links "
                    "WHERE link_id = %s", (link_id,))
        row = cur.fetchone()
    if not row:
        return False
    return (int(row["amount_minor"]) == int(amount_minor)
            and (row["currency"] or "").upper() == (currency or "").upper())


def mark_sql_ready(payment_id: str):
    with db_cursor() as cur:
        cur.execute("UPDATE ow_payment_events SET applied_sql = 1 "
                    "WHERE payment_id = %s", (payment_id,))


def paid_events(limit: int = 100):
    """Verified paid callbacks, newest first — the operator's work queue."""
    _ensure_table()
    with db_cursor() as cur:
        cur.execute("""SELECT e.*, l.site, l.draft_id
                       FROM ow_payment_events e
                       LEFT JOIN ow_payment_links l ON l.link_id = e.link_id
                       WHERE e.verified = 1 AND e.payment_id IS NOT NULL
                       ORDER BY e.id DESC LIMIT %s""", (int(limit),))
        return cur.fetchall()


def payment_sql(order_id: str, payment_id: str, amount_minor: int,
                currency: str, link_id: str = "") -> str:
    """SQL for the operator to record the payment on optiwar2.

    Mirrors what the storefront's /razorpay/verify handler writes, except the
    status row is appended instead of updated — the storefront's in-place UPDATE
    rewrites every historical row and can turn a Complete back into Processed.
    Stock deduction and sales_log are NOT covered here; only the storefront
    pipeline does those."""
    amount = (amount_minor or 0) / MINOR_UNITS
    amount_txt = f"{amount:.2f}"
    dump = json.dumps({"razorpay_payment_id": payment_id,
                       "razorpay_payment_link_id": link_id,
                       "gateway": "razorpay", "source": "ops_payment_link",
                       "amount": amount_txt, "currency": currency})
    return "\n".join([
        f"-- Payment received for {order_id} via Razorpay ({currency} {amount_txt})",
        f"-- razorpay payment id: {payment_id}",
        "-- Signature-verified webhook; recorded in oslb6.ow_payment_events.",
        "-- NOTE: stock deduction and sales_log are handled by the storefront's",
        "-- paid-order pipeline, not by this script.",
        "",
        "START TRANSACTION;",
        f"SET @order_id = '{order_id}';",
        "",
        "-- payment_collector.order_id is a FK to orders(order_id); payment_ref is UNIQUE",
        "INSERT INTO payment_collector (order_id, payment_ref, payment_dump, status)",
        f"VALUES (@order_id, '{payment_id}', '{dump}', 'TXN_SUCCESS');",
        "",
        "-- append-only: never UPDATE order_status",
        "INSERT INTO order_status (order_status_name, order_id, source, manual_flag, note)",
        f"VALUES ('Processed', @order_id, 'ops', 0, 'payment received - {payment_id}');",
        "",
        "INSERT INTO order_history (order_history_content, order_id)",
        f"VALUES ('Payment received via Razorpay - {payment_id}', @order_id);",
        "",
        "-- orders.fulfillment_status is left at 'pending' on purpose: the enum is",
        "-- ('pending','fulfilled','refund_pending') and 'fulfilled' means stock was",
        "-- deducted, which this script does not do.",
        "",
        "COMMIT;",
        "",
        "-- Verification",
        "SELECT id, order_id, payment_ref, status, date_created",
        "FROM payment_collector WHERE order_id = @order_id;",
        "SELECT order_status_id, order_status_name, created_at, source, note",
        "FROM order_status WHERE order_id = @order_id ORDER BY order_status_id;",
    ])
