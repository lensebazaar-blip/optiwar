"""Reship of a returned Optiwar parcel (India reship pilot).

Optiwar owns the reship state machine (RETURNING -> RETURNED -> PAID ->
RESHIPPED), the Rs 250 payment and all customer messaging. This side only
reads that state from Optiwar's queue API, books the new courier AWB once the
order is PAID, and keeps its own receipt / reship records in the ops DB.
Nothing here writes order_status, order_history or customer notifications.
"""
import json
import logging
import os

import requests

import config
from database import db_cursor, ow_cursor

log = logging.getLogger(__name__)

CONDITIONS = ("OK - intact", "Damaged", "Incomplete / items missing", "Not opened yet")
QUEUE_PATH = "/ops/api/reship/queue"
TIMEOUT = 8


def _base():
    return (os.getenv("OPTIWAR_API_BASE") or getattr(config, "OPTIWAR_API_BASE", "")
            or "https://optiwar.in").rstrip("/")


def _token():
    return (os.getenv("OPS_API_TOKEN") or os.getenv("OPS_RESHIP_API_TOKEN")
            or os.getenv("OPS_REFUND_API_TOKEN")
            or getattr(config, "OPS_REFUND_API_TOKEN", ""))


def configured():
    return bool(_token())


def init_tables():
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_return_receipts (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ow_order_id VARCHAR(40) NOT NULL,
                awb VARCHAR(40) DEFAULT '',
                courier VARCHAR(30) DEFAULT '',
                parcel_condition VARCHAR(60) NOT NULL,
                return_reason VARCHAR(255) DEFAULT '',
                remarks TEXT,
                received_by INT,
                received_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX (ow_order_id)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_reships (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ow_order_id VARCHAR(40) NOT NULL,
                reference VARCHAR(60) NOT NULL,
                original_awb_row_id INT,
                original_awb VARCHAR(40) DEFAULT '',
                original_courier VARCHAR(30) DEFAULT '',
                new_awb_row_id INT,
                new_awb VARCHAR(40) DEFAULT '',
                new_courier VARCHAR(30) DEFAULT '',
                reship_uuid VARCHAR(80) DEFAULT '',
                optiwar_status VARCHAR(20) DEFAULT '',
                optiwar_paid_at VARCHAR(40) DEFAULT '',
                created_by INT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX (ow_order_id)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_reship_checks (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ow_order_id VARCHAR(40) NOT NULL,
                status_seen VARCHAR(20) DEFAULT '',
                payment_status VARCHAR(20) DEFAULT '',
                http_status INT,
                error VARCHAR(255) DEFAULT '',
                context VARCHAR(30) DEFAULT '',
                checked_by INT,
                checked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX (ow_order_id)
            )""")
    init_sync_tables()
    init_webhook_table()


class QueueError(Exception):
    pass


def fetch_queue():
    """Raw queue from Optiwar: {'ok':..,'returning':[..],'active':[..]}"""
    if not configured():
        raise QueueError("OPS_API_TOKEN not set")
    r = requests.get(_base() + QUEUE_PATH, timeout=TIMEOUT,
                     headers={"Authorization": "Bearer %s" % _token(),
                              "Accept": "application/json"})
    try:
        data = r.json()
    except ValueError:
        raise QueueError("HTTP %s, non-JSON reply" % r.status_code)
    if r.status_code != 200 or not data.get("ok", r.status_code == 200):
        raise QueueError("HTTP %s: %s" % (r.status_code, data.get("error") or data))
    data["_http"] = r.status_code
    return data


def _log_check(order_id, status, payment, http, error, context, user_id):
    try:
        with db_cursor() as cur:
            cur.execute("""INSERT INTO ow_reship_checks (ow_order_id, status_seen,
                           payment_status, http_status, error, context, checked_by)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (order_id, status or "", payment or "", http, (error or "")[:255],
                         context, user_id))
    except Exception as e:
        log.warning("reship check log failed: %s", e)


def status_for(order_id, user_id=None, context="page"):
    """Optiwar's view of this order's reship. Returns dict:
    {available, status, paid, entry, returning, error}. Every call is logged."""
    out = {"available": False, "status": "", "paid": False, "entry": None,
           "returning": None, "error": ""}
    oid = str(order_id).strip().upper()
    try:
        q = fetch_queue()
    except (QueueError, requests.RequestException) as e:
        out["error"] = str(e)
        _log_check(oid, "", "", None, out["error"], context, user_id)
        return out
    out["available"] = True
    for e in q.get("active") or []:
        if str(e.get("order_id", "")).strip().upper() == oid:
            out["entry"] = e
            out["status"] = str(e.get("status") or "").upper()
            out["paid"] = out["status"] == "PAID"
            break
    for e in q.get("returning") or []:
        if str(e.get("order_id", "")).strip().upper() == oid:
            out["returning"] = e
            if not out["status"]:
                out["status"] = "RETURNING"
            break
    pay = (out["entry"] or {}).get("payment_status", "")
    _log_check(oid, out["status"], pay, q.get("_http"), "", context, user_id)
    out["deadline"] = deadline_info(out["entry"]) if out["entry"] else None
    out["notifications"] = notification_info(out["entry"]) if out["entry"] else {}
    return out


def paid_queue(user_id=None):
    """Active reships Optiwar reports as PAID (ready to book). Empty list on any
    error; the check is logged per order."""
    try:
        q = fetch_queue()
    except (QueueError, requests.RequestException) as e:
        _log_check("*", "", "", None, str(e), "processing", user_id)
        return []
    out = []
    for e in q.get("active") or []:
        st = str(e.get("status") or "").upper()
        oid = str(e.get("order_id", "")).strip().upper()
        if st == "PAID" and oid:
            _log_check(oid, st, e.get("payment_status", ""), q.get("_http"), "",
                       "processing", user_id)
            out.append(e)
    return out


LIST_LABELS = {
    "confirm": ("RETURNING — CONFIRM RECEIPT", "#d97706"),
    "awaiting_payment": ("RETURNED — AWAITING ₹250", "#b45309"),
    "paid": ("RESHIP PAID — READY TO SHIP", "#15803d"),
    "expiring": ("⚠ ABANDONMENT APPROACHING", "#b91c1c"),
    "reshipped": ("RESHIPPED", "#2563eb"),
    "abandoned": ("ABANDONED — AWAITING DISPOSITION", "#7f1d1d"),
    "exception": ("RETURN — SYNC PENDING", "#b91c1c"),
}


def lifecycle_map(user_id=None, context="processing"):
    """{order_id: {...}} for every order in Optiwar's return lifecycle, so
    order lists can show the live state instead of a bare 'Returned'."""
    out = {}
    try:
        q = fetch_queue()
    except (QueueError, requests.RequestException) as e:
        _log_check("*", "", "", None, str(e), context, user_id)
        return out
    failed = {s["ow_order_id"].upper() for s in pending_syncs()}
    for e in q.get("active") or []:
        oid = str(e.get("order_id", "")).strip().upper()
        if not oid:
            continue
        st = str(e.get("status") or "").upper()
        dl = deadline_info(e)
        b = bucket_for(st, dl, oid in failed)
        label, color = LIST_LABELS[b]
        if b == "expiring":
            label = "⚠ ABANDONMENT IN %s DAYS" % dl["days_remaining"]
        out[oid] = {"status": st, "bucket": b, "label": label, "color": color,
                    "entry": e, "deadline": dl, "paid": st == "PAID"}
        if st == "PAID":
            _log_check(oid, st, e.get("payment_status", ""), q.get("_http"), "",
                       context, user_id)
    for e in q.get("returning") or []:
        oid = str(e.get("order_id", "")).strip().upper()
        if oid and oid not in out:
            b = "exception" if oid in failed else "confirm"
            label, color = LIST_LABELS[b]
            out[oid] = {"status": "RETURNING", "bucket": b, "label": label,
                        "color": color, "entry": e, "deadline": None, "paid": False}
    return out


_COUNT_CACHE = {"at": 0.0, "n": 0}


def ready_to_ship_count(max_age=60):
    """Number of PAID reships waiting for Ops to book (cached for the nav badge)."""
    import time
    now = time.time()
    if now - _COUNT_CACHE["at"] > max_age:
        try:
            q = fetch_queue()
            _COUNT_CACHE["n"] = sum(
                1 for e in q.get("active") or []
                if str(e.get("status") or "").upper() == "PAID")
        except (QueueError, requests.RequestException):
            pass
        _COUNT_CACHE["at"] = now
    return _COUNT_CACHE["n"]


def booked_for(order_id, st):
    """Local reship already booked against Optiwar's current reship record (same
    reship_uuid), i.e. the AWB still has to be typed into Record reshipment."""
    uuid = str((st.get("entry") or {}).get("reship_uuid") or "")
    if not uuid:
        return None
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_reships WHERE ow_order_id = %s AND reship_uuid = %s "
                    "AND new_awb <> '' ORDER BY id DESC LIMIT 1", (order_id, uuid))
        row = cur.fetchone()
    if not row:
        return None
    with ow_cursor() as cur:
        cur.execute("SELECT awb_status FROM ops_shipping_awb WHERE id = %s", (row["new_awb_row_id"],))
        awb = cur.fetchone()
    if awb and awb.get("awb_status") == "cancelled":
        return None
    return row


def next_reference(order_id):
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM ow_reships WHERE ow_order_id = %s "
                    "AND new_awb <> ''", (order_id,))
        n = (cur.fetchone() or {}).get("n", 0) or 0
    return "OW-%s-R%d" % (order_id, n + 1)


def record(order_id, reference, original, new_row_id, new_awb, new_courier, st, user_id):
    entry = st.get("entry") or {}
    with db_cursor() as cur:
        cur.execute("""INSERT INTO ow_reships (ow_order_id, reference, original_awb_row_id,
                       original_awb, original_courier, new_awb_row_id, new_awb, new_courier,
                       reship_uuid, optiwar_status, optiwar_paid_at, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (order_id, reference,
                     (original or {}).get("id"), (original or {}).get("tracking_number") or "",
                     (original or {}).get("courier") or "",
                     new_row_id, new_awb, new_courier,
                     str(entry.get("reship_uuid") or ""), st.get("status") or "",
                     str(entry.get("paid_at") or ""), user_id))
        return cur.lastrowid


def list_reships(order_id):
    with db_cursor() as cur:
        cur.execute("""SELECT r.*, u.full_name AS created_by_name FROM ow_reships r
                       LEFT JOIN platform_users u ON u.id = r.created_by
                       WHERE r.ow_order_id = %s ORDER BY r.id DESC""", (order_id,))
        return cur.fetchall()


def save_receipt(order_id, awb, courier, condition, reason, remarks, user_id):
    if condition not in CONDITIONS:
        raise ValueError("Pick a parcel condition")
    with db_cursor() as cur:
        cur.execute("""INSERT INTO ow_return_receipts (ow_order_id, awb, courier,
                       parcel_condition, return_reason, remarks, received_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (order_id, awb or "", courier or "", condition,
                     (reason or "").strip()[:255], (remarks or "").strip(), user_id))
        return cur.lastrowid


def list_receipts(order_id):
    with db_cursor() as cur:
        cur.execute("""SELECT r.*, u.full_name AS received_by_name FROM ow_return_receipts r
                       LEFT JOIN platform_users u ON u.id = r.received_by
                       WHERE r.ow_order_id = %s ORDER BY r.id DESC""", (order_id,))
        return cur.fetchall()


# --------------------------------------------------------------------------
# Optiwar synchronisation: return-received and ship, with idempotent retry
# --------------------------------------------------------------------------

RETURN_RECEIVED_PATH = "/ops/api/shipments/%s/return-received"
SHIP_PATH = "/ops/api/reshipments/%s/ship"
HOLDING_DAYS = 60
EXPIRY_WARN_DAYS = 7


def _operator(user):
    return (user or {}).get("email") or (user or {}).get("username") \
        or (user or {}).get("full_name") or "ops-user-%s" % (user or {}).get("id")


def _post(path, body):
    """POST to Optiwar's Ops API. Optiwar's CSRF guard wants an Origin on every
    POST even with a Bearer token, so the API's own origin is sent."""
    if not configured():
        raise QueueError("OPS_API_TOKEN not set")
    base = _base()
    r = requests.post(base + path, json=body, timeout=TIMEOUT,
                      headers={"Authorization": "Bearer %s" % _token(),
                               "Accept": "application/json", "Origin": base})
    try:
        data = r.json()
    except ValueError:
        data = {"ok": False, "error": "http_%s" % r.status_code,
                "message": (r.text or "")[:200]}
    return r.status_code, data


def init_sync_tables():
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_reship_sync (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ow_order_id VARCHAR(40) NOT NULL,
                kind VARCHAR(20) NOT NULL,
                reship_uuid VARCHAR(80) DEFAULT '',
                local_ref_id INT,
                payload TEXT,
                state VARCHAR(12) NOT NULL DEFAULT 'pending',
                attempts INT NOT NULL DEFAULT 0,
                http_status INT,
                last_error VARCHAR(255) DEFAULT '',
                response TEXT,
                created_by INT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_attempt_at DATETIME NULL,
                synced_at DATETIME NULL,
                INDEX (ow_order_id), INDEX (state)
            )""")
        cur.execute("""SELECT CHARACTER_MAXIMUM_LENGTH AS n FROM information_schema.COLUMNS
                       WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ow_reship_sync'
                       AND COLUMN_NAME = 'kind'""")
        col = cur.fetchone()
        if col and int(col["n"] or 0) < 40:
            cur.execute("ALTER TABLE ow_reship_sync MODIFY kind VARCHAR(40) NOT NULL")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_reship_seen (
                reship_uuid VARCHAR(80) PRIMARY KEY,
                ow_order_id VARCHAR(40) NOT NULL,
                last_status VARCHAR(20) DEFAULT '',
                alerted_abandoned TINYINT DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )""")


def init_webhook_table():
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_reship_webhook_events (
                event_id VARCHAR(120) PRIMARY KEY,
                event VARCHAR(60) NOT NULL,
                ow_order_id VARCHAR(40) DEFAULT '',
                reship_uuid VARCHAR(80) DEFAULT '',
                payload TEXT,
                remote_addr VARCHAR(64) DEFAULT '',
                received_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX (ow_order_id), INDEX (received_at)
            )""")
        cur.execute("ALTER TABLE ow_reship_webhook_events "
                    "ADD COLUMN IF NOT EXISTS processed_at DATETIME NULL")
        cur.execute("UPDATE ow_reship_webhook_events SET processed_at = received_at "
                    "WHERE processed_at IS NULL AND event NOT LIKE 'reverse\\_pickup.%%'")


WEBHOOK_SECRET_ENV = "OPTIWAR_RESHIP_WEBHOOK_SECRET"
RP_EVENT_PREFIX = "reverse_pickup."
WEBHOOK_STATUS_BY_EVENT = {
    "shipment.returned_to_ops": "RETURNED",
    "reship.available": "RETURNED",
    "reship.payment_completed": "PAID",
    "reship.shipped": "RESHIPPED",
    "reship.abandoned": "ABANDONED",
    "reship.reopened": "RETURNED",
}


def webhook_secret():
    return (os.getenv(WEBHOOK_SECRET_ENV) or "").strip()


def verify_webhook(body, signature):
    """HMAC-SHA256 of the raw body, as Optiwar sends it in X-Optiwar-Signature
    (``sha256=<hex>``). Fails closed: no secret configured -> nothing verifies."""
    import hashlib
    import hmac
    secret = webhook_secret()
    if not secret or not signature:
        return False
    sig = signature.strip()
    if sig.lower().startswith("sha256="):
        sig = sig[7:]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig.lower())


def record_webhook(payload, remote_addr="", header_event_id=""):
    """Store one Optiwar push exactly once, keyed by X-Optiwar-Event-Id (or the
    body's event_id). Returns (stored, event_id). A repeat delivery is
    acknowledged but changes nothing. reverse_pickup.* events are stored
    unprocessed; the caller folds them in and marks them processed."""
    body_id = str(payload.get("event_id") or "").strip()
    header_event_id = (header_event_id or "").strip()
    if header_event_id and body_id and header_event_id != body_id:
        raise ValueError("X-Optiwar-Event-Id does not match event_id")
    event_id = header_event_id or body_id
    event = str(payload.get("event") or "").strip()
    oid = str(payload.get("order_ref") or payload.get("order_id") or "").strip().upper()
    uuid = str(payload.get("reship_uuid") or payload.get("pickup_uuid") or "").strip()
    if not event_id or not event:
        raise ValueError("event_id and event are required")
    payload = dict(payload, event_id=event_id)
    deferred = event.startswith(RP_EVENT_PREFIX)
    with db_cursor() as cur:
        cur.execute("""INSERT IGNORE INTO ow_reship_webhook_events
                       (event_id, event, ow_order_id, reship_uuid, payload, remote_addr, processed_at)
                       VALUES (%s,%s,%s,%s,%s,%s,""" + ("NULL" if deferred else "NOW()") + ")",
                    (event_id[:120], event[:60], oid[:40], uuid[:80],
                     json.dumps(payload, sort_keys=True), remote_addr[:64]))
        stored = cur.rowcount == 1
    if stored and not deferred:
        _COUNT_CACHE["at"] = 0.0
        st = WEBHOOK_STATUS_BY_EVENT.get(event)
        if st and uuid:
            with db_cursor() as cur:
                cur.execute("""INSERT INTO ow_reship_seen (reship_uuid, ow_order_id, last_status)
                               VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE
                               last_status = VALUES(last_status)""", (uuid, oid, st))
        # The queue stays the authority for the full state; re-reading it here
        # also fires the internal ABANDONED alert through the usual path.
        try:
            returns_board(None)
        except Exception as e:
            log.warning("queue refresh after webhook failed: %s", e)
    return stored, event_id


def webhook_event(event_id):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_reship_webhook_events WHERE event_id = %s", (event_id,))
        return cur.fetchone()


def mark_webhook_processed(event_id):
    with db_cursor() as cur:
        cur.execute("UPDATE ow_reship_webhook_events SET processed_at = NOW() "
                    "WHERE event_id = %s AND processed_at IS NULL", (event_id,))


def recent_webhooks(limit=20):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_reship_webhook_events ORDER BY received_at DESC LIMIT %s",
                    (limit,))
        return cur.fetchall()


def _new_sync(order_id, kind, payload, user_id, reship_uuid="", local_ref_id=None):
    with db_cursor() as cur:
        cur.execute("""INSERT INTO ow_reship_sync (ow_order_id, kind, reship_uuid, local_ref_id,
                       payload, created_by) VALUES (%s,%s,%s,%s,%s,%s)""",
                    (order_id, kind, reship_uuid or "", local_ref_id, json.dumps(payload), user_id))
        return cur.lastrowid


def get_sync(sync_id):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_reship_sync WHERE id = %s", (sync_id,))
        return cur.fetchone()


def syncs_for(order_id):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_reship_sync WHERE ow_order_id = %s ORDER BY id DESC",
                    (order_id,))
        return cur.fetchall()


RESHIP_SYNC_KINDS = ("return_received", "ship")
RP_SYNC_PATHS = {
    "rp_waive": "/ops/api/shipments/%s/reverse-pickup/fee/waive",
    "rp_reason": "/ops/api/shipments/%s/reverse-pickup/reason",
    "rp_received": "/ops/api/shipments/%s/reverse-pickup/received",
    "rp_inspection": "/ops/api/shipments/%s/reverse-pickup/inspection",
    "rp_decision": "/ops/api/shipments/%s/reverse-pickup/request/decision",
    "rp_forward": "/ops/api/shipments/%s/reverse-pickup/forward-shipped",
    "rp_complete": "/ops/api/shipments/%s/reverse-pickup/complete",
}


def pending_syncs(order_id=None, kinds=RESHIP_SYNC_KINDS):
    marks = ",".join(["%s"] * len(kinds))
    with db_cursor() as cur:
        if order_id:
            cur.execute("SELECT * FROM ow_reship_sync WHERE ow_order_id = %s AND state NOT IN ('synced', 'refused') "
                        "AND kind IN (" + marks + ") ORDER BY id DESC", (order_id,) + tuple(kinds))
        else:
            cur.execute("SELECT * FROM ow_reship_sync WHERE state NOT IN ('synced', 'refused') "
                        "AND kind IN (" + marks + ") ORDER BY id DESC", tuple(kinds))
        return cur.fetchall()


OPTIWAR_REFUSALS = {
    "pickup_already_received": "Optiwar has the parcel as received; cancel refused",
    "pickup_cancelled": "pickup was cancelled first; receipt refused",
    "fee_not_settled": "fee is not PAID or WAIVED at Optiwar; booking refused",
    "fee_already_paid": "fee is already PAID at Optiwar; waiver refused",
    "waiver_exists": "Optiwar already has a waiver for this fee",
    "no_request": "no customer return request on this case",
    "decision_exists": "the request was already decided",
    "not_inspected": "inspection not recorded at Optiwar yet",
    "consent_required": "customer consent not recorded yet; nothing may ship back",
    "awb_is_original_shipment": "that AWB is the original outbound shipment",
    "awb_is_reverse_pickup": "that AWB is a reverse-pickup waybill",
    "awb_in_use": "that AWB is already used on another shipment",
    "forward_shipment_exists": "a different forward shipment is already recorded for this case",
    "forward_shipment_required": "no forward shipment on record for this outcome",
    "outcome_mismatch": "outcome does not match the recorded forward shipment",
    "defect_not_confirmed": "PRODUCT_REFUNDED needs a confirmed manufacturing defect",
    "product_refund_not_executed": "no product refund executed in the return-refund console since the case opened",
    "fee_refund_open": "the ₹250 refund is still PENDING/FAILED/EXCEPTION; completion blocked",
    "completed_exists": "the case is already completed with a different outcome",
}


def run_sync(sync_id):
    """Execute (or re-execute) one sync row. Both Optiwar endpoints are
    idempotent for a repeat with the same data: return-received hands back the
    existing reship, /ship returns the row when the AWB already matches. Every
    attempt is recorded; the row becomes 'synced' only on ok=true."""
    row = get_sync(sync_id)
    if not row or row["state"] == "synced":
        return row
    payload = json.loads(row["payload"] or "{}")
    if row["kind"] == "return_received":
        path = RETURN_RECEIVED_PATH % row["ow_order_id"]
    elif row["kind"] == "ship":
        path = SHIP_PATH % row["reship_uuid"]
    elif row["kind"] == "reverse_pickup":
        path = "/ops/api/shipments/%s/reverse-pickup" % row["ow_order_id"]
    elif row["kind"] == "reverse_pickup_cancel":
        path = "/ops/api/shipments/%s/reverse-pickup/cancel" % row["ow_order_id"]
    elif row["kind"] in RP_SYNC_PATHS:
        path = RP_SYNC_PATHS[row["kind"]] % row["ow_order_id"]
    else:
        return row
    http, data, err = None, None, ""
    try:
        http, data = _post(path, payload)
        if not data.get("ok"):
            code = data.get("error") or http
            err = "%s: %s" % (code, OPTIWAR_REFUSALS.get(code) or data.get("message") or "")
    except (QueueError, requests.RequestException) as e:
        err = str(e)
    ok = bool(data and data.get("ok"))
    resh = (data or {}).get("reship") or {}
    with db_cursor() as cur:
        cur.execute("""UPDATE ow_reship_sync SET attempts = attempts + 1, http_status = %s,
                       last_error = %s, response = %s, state = %s, last_attempt_at = NOW(),
                       synced_at = CASE WHEN %s THEN NOW() ELSE synced_at END,
                       reship_uuid = CASE WHEN reship_uuid = '' THEN %s ELSE reship_uuid END
                       WHERE id = %s""",
                    (http, err[:255], json.dumps(data) if data else None,
                     "synced" if ok else "failed", ok,
                     str(resh.get("reship_uuid") or ""), sync_id))
        if ok and row["kind"] == "ship" and row["local_ref_id"]:
            cur.execute("UPDATE ow_reships SET optiwar_status = %s WHERE id = %s",
                        (str(resh.get("status") or "RESHIPPED"), row["local_ref_id"]))
    return get_sync(sync_id)


def confirm_return_received(order_id, user, awb, courier, return_reason, receipt_id=None):
    """Tell Optiwar the parcel is physically at Ops. Local receipt is already
    saved; a failed call leaves a 'failed' sync row for Retry Sync."""
    payload = {"operator": _operator(user)}
    if awb:
        payload["original_awb"] = awb
    if courier:
        payload["courier"] = courier
    if return_reason:
        payload["return_reason"] = return_reason[:255]
    sid = _new_sync(order_id, "return_received", payload, (user or {}).get("id"),
                    local_ref_id=receipt_id)
    return run_sync(sid)


def sync_ship(order_id, reship_row_id, reship_uuid, new_courier, new_awb, user):
    payload = {"operator": _operator(user), "new_courier": new_courier, "new_awb": new_awb}
    sid = _new_sync(order_id, "ship", payload, (user or {}).get("id"),
                    reship_uuid=reship_uuid, local_ref_id=reship_row_id)
    return run_sync(sid)


def has_return_received_sync(order_id):
    with db_cursor() as cur:
        cur.execute("SELECT id, state FROM ow_reship_sync WHERE ow_order_id = %s "
                    "AND kind = 'return_received' ORDER BY id DESC LIMIT 1", (order_id,))
        return cur.fetchone()


# --------------------------------------------------------------------------
# Lifecycle view: buckets, aging, notification status (all from Optiwar)
# --------------------------------------------------------------------------

BUCKETS = (
    ("confirm", "RETURN CONFIRMATION NEEDED"),
    ("awaiting_payment", "AWAITING CUSTOMER PAYMENT"),
    ("paid", "RESHIP PAID — READY TO SHIP"),
    ("expiring", "APPROACHING 60-DAY EXPIRY"),
    ("reshipped", "RESHIPPED"),
    ("abandoned", "ABANDONED"),
    ("exception", "EXCEPTION"),
)


def _parse_dt(v):
    from datetime import datetime
    from email.utils import parsedate_to_datetime
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v)
    try:
        return parsedate_to_datetime(s).replace(tzinfo=None)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def deadline_info(entry):
    """The 60-day holding deadline exactly as Optiwar supplies it
    (returned_at / abandon_at / days_remaining). Nothing is computed here: when
    Optiwar has not sent abandon_at the deadline is shown as not yet supplied."""
    e = entry or {}
    returned_at = _parse_dt(e.get("returned_at") or e.get("ops_return_confirmed_at"))
    abandon_at = _parse_dt(e.get("abandon_at"))
    days = e.get("days_remaining")
    try:
        days = int(days) if days is not None else None
    except (TypeError, ValueError):
        days = None
    st = str(e.get("status") or "").upper()
    on_hold = bool(e.get("on_hold"))
    return {"returned_at": returned_at, "abandon_at": abandon_at, "days_remaining": days,
            "abandoned_at": _parse_dt(e.get("abandoned_at")),
            "supplied": abandon_at is not None or days is not None,
            "holding_days": e.get("holding_days") or HOLDING_DAYS,
            "on_hold": on_hold, "hold_reason": e.get("hold_reason") or "",
            "days_held": e.get("days_held"),
            "warning": (not on_hold and st not in ("PAID", "RESHIPPED")
                        and days is not None and days <= EXPIRY_WARN_DAYS)}


def notification_info(entry):
    """Customer notification outcome as reported by Optiwar, if the queue
    carries it (``notifications`` map or ``notify_*`` fields)."""
    e = entry or {}
    n = e.get("notifications")
    out = {}
    if isinstance(n, dict):
        for ch in ("email", "whatsapp", "sms"):
            v = n.get(ch)
            if isinstance(v, dict):
                v = v.get("status")
            if v:
                out[ch] = str(v).upper()
    for ch in ("email", "whatsapp", "sms"):
        v = e.get("notify_%s" % ch) or e.get("%s_status" % ch)
        if v and ch not in out:
            out[ch] = str(v).upper()
    ns = e.get("notification_state")
    if isinstance(ns, dict) and not out:
        for s in ns.get("sent") or []:
            ch = str((s or {}).get("channel") or "").lower()
            if ch:
                out[ch] = "SENT"
        try:
            failed = int(ns.get("failed") or 0)
        except (TypeError, ValueError):
            failed = 0
        if failed and not out:
            out["customer"] = "FAILED"
        elif not out and ns.get("day0_sent") is False:
            out["customer"] = "QUEUED"
        if failed and out:
            out["failed"] = "%d FAILED" % failed
    return out


def bucket_for(status, dl, sync_failed=False):
    st = (status or "").upper()
    if sync_failed:
        return "exception"
    if st == "RETURNING":
        return "confirm"
    if st in ("RETURNED", "PAYMENT_PENDING"):
        return "expiring" if dl.get("warning") else "awaiting_payment"
    if st == "PAID":
        return "paid"
    if st == "RESHIPPED":
        return "reshipped"
    if st == "ABANDONED":
        return "abandoned"
    return "exception"


def _customer_head(order_ids):
    if not order_ids:
        return {}
    ph = ",".join(["%s"] * len(order_ids))
    with ow_cursor() as cur:
        cur.execute(f"""SELECT o.order_id, MAX(c.customer_name) AS customer,
                               MAX(c.customer_email) AS email, SUM(o.order_total) AS total
                        FROM orders o LEFT JOIN customers c ON o.customer_id = c.customer_id
                        WHERE o.order_id IN ({ph}) GROUP BY o.order_id""", tuple(order_ids))
        return {r["order_id"]: r for r in cur.fetchall()}


def returns_board(user_id=None):
    """Every Optiwar return in one state-driven list, bucketed. Also notes
    status transitions (for the ABANDONED internal alert)."""
    out = {"error": "", "rows": [], "buckets": {k: [] for k, _ in BUCKETS}}
    try:
        q = fetch_queue()
    except (QueueError, requests.RequestException) as e:
        out["error"] = str(e)
        _log_check("*", "", "", None, out["error"], "returns", user_id)
        return out
    failed = {}
    for s in pending_syncs():
        failed.setdefault(s["ow_order_id"], []).append(s)
    entries = []
    for e in q.get("active") or []:
        entries.append((str(e.get("status") or "").upper(), e))
    for e in q.get("returning") or []:
        entries.append(("RETURNING", e))
    heads = _customer_head(sorted({str(e.get("order_id")) for _, e in entries}))
    for st, e in entries:
        oid = str(e.get("order_id") or "").strip().upper()
        dl = deadline_info(e) if st != "RETURNING" else deadline_info({})
        pend = failed.get(oid, [])
        # a return-received sync still pending means Ops has the parcel already
        if st == "RETURNING" and any(s["kind"] == "return_received" for s in pend):
            st_shown = "RETURN RECEIVED LOCALLY"
        else:
            st_shown = st
        b = bucket_for(st, dl, sync_failed=bool(pend))
        head = heads.get(oid) or {}
        row = {"order_id": oid, "status": st, "status_shown": st_shown, "bucket": b,
               "entry": e, "deadline": dl, "notifications": notification_info(e),
               "pending_syncs": pend, "customer": head.get("customer") or "",
               "email": head.get("email") or "", "total": float(head.get("total") or 0),
               "awb": e.get("original_awb") or e.get("awb") or "",
               "courier": e.get("original_courier") or e.get("courier") or "",
               "new_awb": e.get("new_awb") or "", "new_courier": e.get("new_courier") or "",
               "paid_at": e.get("paid_at") or "", "reship_uuid": e.get("reship_uuid") or ""}
        out["rows"].append(row)
        out["buckets"][b].append(row)
    _note_transitions(out["rows"])
    return out


def _note_transitions(rows):
    """Remember the last status seen per reship; on first sight of ABANDONED
    send the internal alert once."""
    for r in rows:
        uuid = r.get("reship_uuid")
        if not uuid:
            continue
        try:
            with db_cursor() as cur:
                cur.execute("SELECT last_status, alerted_abandoned FROM ow_reship_seen "
                            "WHERE reship_uuid = %s", (uuid,))
                prev = cur.fetchone()
                cur.execute("""INSERT INTO ow_reship_seen (reship_uuid, ow_order_id, last_status)
                               VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE last_status = VALUES(last_status),
                               ow_order_id = VALUES(ow_order_id)""", (uuid, r["order_id"], r["status"]))
            if r["status"] == "ABANDONED" and not (prev or {}).get("alerted_abandoned"):
                if _alert_abandoned(r):
                    with db_cursor() as cur:
                        cur.execute("UPDATE ow_reship_seen SET alerted_abandoned = 1 "
                                    "WHERE reship_uuid = %s", (uuid,))
        except Exception as e:
            log.warning("reship transition note failed: %s", e)


def _alert_abandoned(r):
    from services import notifications
    to = os.getenv("OPS_ALERT_EMAIL") or getattr(config, "OPS_ALERT_EMAIL", "") \
        or "admin@lensbazaar.com"
    cc = os.getenv("OPTIWAR_OPS_ALERT_EMAIL") or getattr(config, "OPTIWAR_ADMIN_CC", "")
    dl = r["deadline"]
    notif = ", ".join("%s: %s" % (k, v) for k, v in r["notifications"].items()) or "not reported"
    lines = [
        "Optiwar Returned Package — ABANDONED",
        "Order: OW-%s" % r["order_id"],
        "Customer: %s" % (r["customer"] or ""),
        "Returned to Ops: %s" % (dl["returned_at"] or r["entry"].get("ops_return_confirmed_at") or ""),
        "60-day holding period expired: %s" % (r["entry"].get("abandoned_at") or dl["abandon_at"] or ""),
        "Customer notifications: %s" % notif,
        "Reship payment: Not received",
        "Status: ABANDONED — awaiting approved disposition process.",
        "Original AWB: %s %s" % (r["courier"], r["awb"]),
    ]
    text = "\n".join(lines)
    html = "<pre style='font:13px monospace'>%s</pre>" % text
    try:
        notifications.send_email(to, "Optiwar returned package ABANDONED — OW-%s" % r["order_id"],
                                 html, text, cc=cc or None)
        return True
    except Exception as e:
        log.warning("abandoned alert email failed for %s: %s", r["order_id"], e)
        return False
