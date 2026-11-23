"""Reverse-pickup lifecycle around the Delhivery booking: pay-first fee gate,
audited waiver, parcel received, inspection, defect refund approval, customer
consent, new forward dispatch and completion.

Money, refunds, customer consent and customer messages are Optiwar's: their
state only ever comes from Optiwar API responses or signed reverse_pickup.*
webhooks (ow_rp_fee). Ops records the physical facts and decisions (who, when,
what) and tells Optiwar through retryable ow_reship_sync rows."""
import json
import logging
import os
import time
from pathlib import Path

import requests

from database import db_cursor, ow_cursor
from services import delhivery
from services import dtdc
from services import reship
from services import optiwar_refunds

log = logging.getLogger(__name__)

FEE_STATES = ("DUE", "PAID", "WAIVED", "REFUNDED", "PARTIALLY_REFUNDED")
BOOKABLE_FEE = ("PAID", "WAIVED")
WAIVER_CODES = (
    ("OWNER_DECISION", "Owner decision"),
    ("GOODWILL", "Goodwill"),
    ("DEFECT_EVIDENT_PRE_PICKUP", "Defect evident before pickup"),
    ("PRE_EXISTING_CASE", "Pre-existing case"),
    ("BOOKED_BEFORE_FEE_FLOW", "Booked before the fee flow"),
    ("OTHER", "Other (explain)"),
)
CONDITIONS = ("Intact", "Damaged packaging", "Product damaged", "Wrong item", "Empty/missing")
DEFAULT_FEE_MINOR = 25000
EVIDENCE_DIR = Path(os.getenv("RP_EVIDENCE_DIR", "/var/www/lbws/order-platform/rp_evidence"))
EVIDENCE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".pdf", ".mp4")
EVIDENCE_MAX_BYTES = 15 * 1024 * 1024
STATE_PATH = "/ops/api/shipments/%s/reverse-pickup"
QUEUE_PATH = "/ops/api/reverse-pickup/queue"
SYNC_KINDS = tuple(reship.RP_SYNC_PATHS)
ALL_SYNC_KINDS = SYNC_KINDS + ("reverse_pickup", "reverse_pickup_cancel")

BUCKETS = (
    ("review", "RETURN REQUESTED — AWAITING REVIEW"),
    ("awaiting_fee", "AWAITING ₹250"),
    ("ready_to_book", "READY TO BOOK PICKUP"),
    ("booked", "PICKUP BOOKED"),
    ("in_transit", "RETURN IN TRANSIT"),
    ("inspect", "RETURN RECEIVED — INSPECTION REQUIRED"),
    ("refund", "DEFECT CONFIRMED — REFUND REQUIRED"),
    ("consent", "NO DEFECT — AWAITING CUSTOMER CONSENT"),
    ("forward", "READY FOR FORWARD DISPATCH"),
    ("shipped", "FORWARD SHIPPED"),
    ("complete", "COMPLETE"),
    ("abandoned", "ABANDONED — NO CUSTOMER REPLY IN 60 DAYS"),
    ("exception", "EXCEPTION"),
)
DECISIONS = (
    ("APPROVED_SUBJECT_TO_INSPECTION", "Approve, subject to inspection"),
    ("FURTHER_INFORMATION_REQUIRED", "Ask the customer for more information"),
    ("NOT_APPROVED", "Not approved"),
)
SHIPMENT_TYPES = {
    1: (("REPLACEMENT", "Replacement"), ("REPAIRED_ORIGINAL", "Repaired original")),
    0: (("ORIGINAL_RETURNED", "Original returned as is"),),
}
OUTCOME_FOR_TYPE = {"REPLACEMENT": "REPLACEMENT_SHIPPED",
                    "REPAIRED_ORIGINAL": "REPAIRED_ORIGINAL_SHIPPED",
                    "ORIGINAL_RETURNED": "ORIGINAL_RETURNED"}
REQUEST_BUCKET = {"REQUESTED": "review", "INFO_REQUESTED": "review",
                  "AWAITING_FEE": "awaiting_fee", "READY_TO_BOOK": "ready_to_book"}
REQUEST_GONE = ("NOT_APPROVED", "COMPLETED")
BUCKET_LABEL = dict(BUCKETS)


LIVE_ENV = "OPTIWAR_RP_LIVE"
LIVE_STEPS = ("state", "queue", "waive", "reason", "received", "inspection", "decision",
              "forward", "complete")


def live_steps():
    """Optiwar endpoints confirmed production-live (env OPTIWAR_RP_LIVE,
    comma-separated, or 'all'). Actions whose endpoint is not live are refused
    and hidden, so nothing is recorded that Optiwar cannot receive."""
    items = {x.strip().lower() for x in os.getenv(LIVE_ENV, "").split(",") if x.strip()}
    return set(LIVE_STEPS) if "all" in items else items & set(LIVE_STEPS)


def is_live(step):
    return step in live_steps()


class LifecycleError(Exception):
    pass


def _require_live(step):
    if not is_live(step):
        raise LifecycleError("Optiwar's %s endpoint is not live yet - nothing was recorded" % step)


def init_tables():
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_rp_lifecycle (
                pickup_id INT PRIMARY KEY,
                ow_order_id VARCHAR(40) NOT NULL,
                received_at DATETIME NULL, received_by INT NULL,
                parcel_condition VARCHAR(40) DEFAULT '', received_notes VARCHAR(500) DEFAULT '',
                inspected_at DATETIME NULL, inspected_by INT NULL, defect TINYINT NULL,
                inspection_remarks VARCHAR(1000) DEFAULT '',
                refund_amount_minor INT NULL, refund_note VARCHAR(500) DEFAULT '',
                refund_approved_by INT NULL, refund_approved_at DATETIME NULL,
                fwd_courier VARCHAR(20) DEFAULT '', fwd_awb VARCHAR(40) DEFAULT '',
                fwd_awb_row_id INT NULL, fwd_booked_by INT NULL, fwd_booked_at DATETIME NULL,
                fwd_dispatch_by INT NULL, fwd_dispatch_at DATETIME NULL,
                completed_at DATETIME NULL, completed_by INT NULL,
                outcome VARCHAR(30) DEFAULT '', completion_note VARCHAR(500) DEFAULT '',
                INDEX (ow_order_id)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_rp_fee (
                ow_order_id VARCHAR(40) PRIMARY KEY,
                fee_state VARCHAR(24) DEFAULT '',
                amount_minor INT NULL, currency VARCHAR(8) DEFAULT 'INR',
                refunded_minor INT DEFAULT 0,
                razorpay_payment_id VARCHAR(60) DEFAULT '',
                refund_id VARCHAR(80) DEFAULT '', refund_state VARCHAR(24) DEFAULT '',
                razorpay_refund_id VARCHAR(60) DEFAULT '',
                waiver_code VARCHAR(40) DEFAULT '',
                consent_at DATETIME NULL, consent_by VARCHAR(191) DEFAULT '',
                optiwar_pickup_uuid VARCHAR(40) DEFAULT '', request_id VARCHAR(40) DEFAULT '',
                declared_reason VARCHAR(255) DEFAULT '',
                source VARCHAR(80) DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )""")
        for col in ("waiver_by VARCHAR(191) DEFAULT ''", "waiver_at VARCHAR(19) DEFAULT ''",
                    "waiver_note VARCHAR(500) DEFAULT ''", "ow_received_at VARCHAR(19) DEFAULT ''",
                    "notifications TEXT NULL", "queue_state VARCHAR(40) DEFAULT ''",
                    "ow_label VARCHAR(80) DEFAULT ''", "booking_allowed TINYINT NULL",
                    "request_json TEXT NULL", "refunds_json TEXT NULL", "forward_json TEXT NULL",
                    "completion_json TEXT NULL", "hold_json TEXT NULL"):
            cur.execute("ALTER TABLE ow_rp_fee ADD COLUMN IF NOT EXISTS " + col)
        cur.execute("ALTER TABLE ow_rp_lifecycle ADD COLUMN IF NOT EXISTS "
                    "fwd_type VARCHAR(24) DEFAULT ''")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_rp_waivers (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ow_order_id VARCHAR(40) NOT NULL,
                pickup_id INT NULL,
                reason_code VARCHAR(40) NOT NULL,
                note VARCHAR(500) DEFAULT '',
                created_by INT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX (ow_order_id)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_rp_evidence (
                id INT AUTO_INCREMENT PRIMARY KEY,
                pickup_id INT NOT NULL,
                ow_order_id VARCHAR(40) NOT NULL,
                filename VARCHAR(200) NOT NULL,
                path VARCHAR(400) NOT NULL,
                uploaded_by INT,
                uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX (pickup_id)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_rp_events (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ow_order_id VARCHAR(40) NOT NULL,
                pickup_id INT NULL,
                event VARCHAR(60) NOT NULL,
                source VARCHAR(10) NOT NULL DEFAULT 'ops',
                actor INT NULL,
                data TEXT,
                at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX (ow_order_id), INDEX (pickup_id)
            )""")


def _event(order_id, pickup_id, event, data=None, user=None, source="ops"):
    with db_cursor() as cur:
        cur.execute("""INSERT INTO ow_rp_events (ow_order_id, pickup_id, event, source, actor, data)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (order_id, pickup_id, event, source, (user or {}).get("id"),
                     json.dumps(data or {}, sort_keys=True, default=str)))


def events_for(order_id):
    with db_cursor() as cur:
        cur.execute("""SELECT e.*, u.full_name AS actor_name FROM ow_rp_events e
                       LEFT JOIN platform_users u ON u.id = e.actor
                       WHERE e.ow_order_id = %s ORDER BY e.at, e.id""", (order_id,))
        rows = cur.fetchall()
    for r in rows:
        try:
            r["data"] = json.loads(r["data"] or "{}")
        except ValueError:
            r["data"] = {}
    return rows


def _pickup(pickup_id):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_reverse_pickups WHERE id = %s", (pickup_id,))
        return cur.fetchone()


def life_for(pickup_id):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_rp_lifecycle WHERE pickup_id = %s", (pickup_id,))
        return cur.fetchone() or {}


def _ensure_life(p):
    with db_cursor() as cur:
        cur.execute("INSERT IGNORE INTO ow_rp_lifecycle (pickup_id, ow_order_id) VALUES (%s,%s)",
                    (p["id"], p["ow_order_id"]))


def fee_for(order_id):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_rp_fee WHERE ow_order_id = %s", (order_id,))
        return cur.fetchone() or {}


def waivers_for(order_id):
    with db_cursor() as cur:
        cur.execute("""SELECT w.*, u.full_name AS created_by_name FROM ow_rp_waivers w
                       LEFT JOIN platform_users u ON u.id = w.created_by
                       WHERE w.ow_order_id = %s ORDER BY w.id DESC""", (order_id,))
        return cur.fetchall()


def evidence_for(pickup_id):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_rp_evidence WHERE pickup_id = %s ORDER BY id", (pickup_id,))
        return cur.fetchall()


def get_evidence(evidence_id):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_rp_evidence WHERE id = %s", (evidence_id,))
        return cur.fetchone()


# --------------------------------------------------------------------------
# Optiwar-authoritative fee / consent projection
# --------------------------------------------------------------------------

def _int_or_none(v):
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def apply_fee(order_id, fee, source):
    """Store Optiwar's fee block. Only called with data Optiwar sent."""
    if not isinstance(fee, dict):
        return
    state = str(fee.get("state") or "").upper()
    if state and state not in FEE_STATES:
        log.warning("unknown fee state %r from Optiwar for %s", state, order_id)
    refunds = [r for r in (fee.get("refunds") or []) if isinstance(r, dict)]
    last = refunds[-1] if refunds else {}
    waiver = fee.get("waiver") if isinstance(fee.get("waiver"), dict) else {}
    with db_cursor() as cur:
        cur.execute("""INSERT INTO ow_rp_fee (ow_order_id, fee_state, amount_minor, currency,
                       refunded_minor, razorpay_payment_id, refund_id, refund_state,
                       razorpay_refund_id, waiver_code, source)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON DUPLICATE KEY UPDATE
                       fee_state = IF(VALUES(fee_state) = '', fee_state, VALUES(fee_state)),
                       amount_minor = COALESCE(VALUES(amount_minor), amount_minor),
                       currency = VALUES(currency),
                       refunded_minor = COALESCE(VALUES(refunded_minor), refunded_minor),
                       razorpay_payment_id = IF(VALUES(razorpay_payment_id) = '',
                                                razorpay_payment_id, VALUES(razorpay_payment_id)),
                       refund_id = IF(VALUES(refund_id) = '', refund_id, VALUES(refund_id)),
                       refund_state = IF(VALUES(refund_state) = '', refund_state, VALUES(refund_state)),
                       razorpay_refund_id = IF(VALUES(razorpay_refund_id) = '',
                                               razorpay_refund_id, VALUES(razorpay_refund_id)),
                       waiver_code = IF(VALUES(waiver_code) = '', waiver_code, VALUES(waiver_code)),
                       source = VALUES(source)""",
                    (order_id, state, _int_or_none(fee.get("amount_minor")),
                     str(fee.get("currency") or "INR")[:8],
                     _int_or_none(fee.get("refunded_minor")),
                     str(fee.get("razorpay_payment_id") or "")[:60],
                     str(last.get("refund_id") or last.get("id") or "")[:80],
                     str(last.get("state") or "").upper()[:24],
                     str(last.get("razorpay_refund_id") or "")[:60],
                     str(waiver.get("reason_code") or "")[:40], source[:80]))
    if "refunds" in fee:
        _set_fee_fields(order_id, source, refunds_json=json.dumps(refunds[-10:]))
    if waiver:
        _set_fee_fields(order_id, source,
                        waiver_by=str(waiver.get("waived_by") or waiver.get("operator") or "")[:191],
                        waiver_at=str(waiver.get("waived_at") or waiver.get("at") or "")[:19],
                        waiver_note=str(waiver.get("note") or "")[:500])


def _set_fee_fields(order_id, source, **fields):
    cols = ", ".join("%s = %%s" % k for k in fields)
    with db_cursor() as cur:
        cur.execute("INSERT IGNORE INTO ow_rp_fee (ow_order_id) VALUES (%s)", (order_id,))
        cur.execute("UPDATE ow_rp_fee SET " + cols + ", source = %s WHERE ow_order_id = %s",
                    tuple(fields.values()) + (source[:80], order_id))


def _apply_optiwar_body(order_id, data, source):
    if not isinstance(data, dict):
        return
    if "fee" in data:
        apply_fee(order_id, data.get("fee"), source)
    refund = data.get("refund")
    if isinstance(refund, dict):
        _set_fee_fields(order_id, source, refund_id=str(refund.get("id") or "")[:80],
                        refund_state=str(refund.get("state") or "").upper()[:24],
                        razorpay_refund_id=str(refund.get("razorpay_refund_id") or "")[:60])
    rp = data.get("reverse_pickup")
    if isinstance(rp, dict):
        if rp.get("id"):
            _set_fee_fields(order_id, source, optiwar_pickup_uuid=str(rp["id"])[:40])
        if rp.get("consent_at"):
            _set_fee_fields(order_id, source, consent_at=str(rp["consent_at"])[:19])
        if rp.get("received_at"):
            _set_fee_fields(order_id, source, ow_received_at=str(rp["received_at"])[:19])
    consent = data.get("consent")
    if isinstance(consent, dict) and (consent.get("at") or consent.get("consent_at")):
        _set_fee_fields(order_id, source,
                        consent_at=str(consent.get("at") or consent.get("consent_at"))[:19],
                        consent_by=str(consent.get("recorded_by") or consent.get("from") or "")[:191])
    if isinstance(data.get("notifications"), list):
        _set_fee_fields(order_id, source, notifications=json.dumps(
            [n for n in data["notifications"] if isinstance(n, dict)][-20:]))
    req = data.get("request")
    if isinstance(req, dict) and req.get("id"):
        _set_fee_fields(order_id, source, request_id=str(req["id"])[:40],
                        declared_reason=str(req.get("declared_reason") or req.get("reason")
                                            or "")[:255])
    if isinstance(req, dict) and "source" in req:
        _set_fee_fields(order_id, source, request_json=json.dumps(req))
    if data.get("queue_state"):
        _set_fee_fields(order_id, source, queue_state=str(data["queue_state"]).upper()[:40],
                        ow_label=str(data.get("label") or "")[:80])
    if "booking_allowed" in data:
        b = data["booking_allowed"]
        _set_fee_fields(order_id, source, booking_allowed=None if b is None else int(bool(b)))
    for key, col in (("forward", "forward_json"), ("completion", "completion_json"),
                     ("hold", "hold_json")):
        if key in data:
            v = data[key]
            _set_fee_fields(order_id, source, **{col: json.dumps(v) if isinstance(v, dict) else None})


def _json(raw, default=None):
    try:
        v = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return v if v is not None else default


def _merge_json(order_id, col, source, updates):
    cur_val = _json(fee_for(order_id).get(col), {})
    if not isinstance(cur_val, dict):
        cur_val = {}
    cur_val.update({k: v for k, v in updates.items() if v is not None})
    _set_fee_fields(order_id, source, **{col: json.dumps(cur_val)})


def _get(path):
    if not reship.configured():
        raise LifecycleError("OPS_API_TOKEN not set")
    r = requests.get(reship._base() + path, timeout=reship.TIMEOUT,
                     headers={"Authorization": "Bearer %s" % reship._token(),
                              "Accept": "application/json"})
    try:
        data = r.json()
    except ValueError:
        raise LifecycleError("HTTP %s, non-JSON reply" % r.status_code)
    if r.status_code != 200 or not data.get("ok"):
        raise LifecycleError("HTTP %s: %s" % (r.status_code, data.get("error") or data))
    return data


def refresh_state(order_id):
    """Re-read Optiwar's reverse-pickup state for one order. Returns an error
    string, or '' when the projection was refreshed."""
    if not is_live("state"):
        return "Optiwar fee-state API not live yet"
    try:
        data = _get(STATE_PATH % order_id)
    except (LifecycleError, requests.RequestException) as e:
        return str(e)[:200]
    _apply_optiwar_body(order_id, data, "GET reverse-pickup")
    return ""


def fee_view(order_id, refresh=False):
    err = refresh_state(order_id) if refresh else ""
    fee = fee_for(order_id)
    state = fee.get("fee_state") or ""
    amount = fee.get("amount_minor")
    return {"state": state, "known": state in FEE_STATES,
            "bookable": state in BOOKABLE_FEE, "amount_minor": amount,
            "amount_text": optiwar_refunds.money(amount, fee.get("currency") or "INR")
            if amount is not None else "", "refunded_minor": fee.get("refunded_minor") or 0,
            "refunded_text": optiwar_refunds.money(fee.get("refunded_minor") or 0, "INR"),
            "refund_state": fee.get("refund_state") or "",
            "refund_ref": fee.get("razorpay_refund_id") or fee.get("refund_id") or "",
            "payment_id": fee.get("razorpay_payment_id") or "",
            "waiver_code": fee.get("waiver_code") or "",
            "consent_at": fee.get("consent_at"), "consent_by": fee.get("consent_by") or "",
            "waiver_by": fee.get("waiver_by") or "", "waiver_at": fee.get("waiver_at") or "",
            "waiver_note": fee.get("waiver_note") or "",
            "ow_received_at": fee.get("ow_received_at") or "",
            "notifications": _notifications(fee.get("notifications")),
            "queue_state": fee.get("queue_state") or "", "ow_label": fee.get("ow_label") or "",
            "booking_allowed": None if fee.get("booking_allowed") is None
            else bool(fee["booking_allowed"]),
            "request": _json(fee.get("request_json")),
            "refunds": [r for r in _json(fee.get("refunds_json"), []) if isinstance(r, dict)],
            "forward": _json(fee.get("forward_json")),
            "completion": _json(fee.get("completion_json")),
            "hold": _json(fee.get("hold_json")),
            "source": fee.get("source") or "", "updated_at": fee.get("updated_at"),
            "error": err}


def _notifications(raw):
    try:
        items = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [n for n in items if isinstance(n, dict)] if isinstance(items, list) else []


def fee_gate(order_id):
    """(allowed, fee_view). Booking is allowed only when Optiwar reports the
    fee PAID or WAIVED."""
    if not is_live("state"):
        fv = dict(fee_view(order_id), bookable=False,
                  error="Optiwar fee-state API not live yet - booking blocked",
                  block_reason="Optiwar fee-state API not live yet")
        return False, fv
    fv = fee_view(order_id, refresh=True)
    if not fv["bookable"]:
        fv["block_reason"] = ("Optiwar shows the ₹250 fee as %s - it must be PAID or WAIVED"
                              % (fv["state"] or "not created"))
        return False, fv
    req = fv["request"] or {}
    if fv["booking_allowed"] is False:
        dec = (req.get("decision") or {}).get("status") if isinstance(req.get("decision"), dict) \
            else None
        fv["block_reason"] = ("Optiwar does not allow booking (%s)" % (
            "request decision: %s" % dec if dec else
            "customer request not approved yet" if req.get("source") == "customer" else
            fv["ow_label"] or "booking_allowed is false"))
        return False, fv
    return True, fv


# --------------------------------------------------------------------------
# Ops actions (each audited and synced to Optiwar)
# --------------------------------------------------------------------------

def _sync(order_id, kind, payload, user, pickup_id):
    sid = reship._new_sync(order_id, kind, payload, (user or {}).get("id"),
                           local_ref_id=pickup_id)
    return after_sync(reship.run_sync(sid))


def _call(order_id, kind, payload, user, pickup_id):
    """A decision with no physical side: Optiwar must accept it first, or
    nothing is recorded and no Retry Sync is left behind."""
    row = _sync(order_id, kind, payload, user, pickup_id)
    if not row or row["state"] != "synced":
        if row:
            with db_cursor() as cur:
                cur.execute("UPDATE ow_reship_sync SET state = 'refused' WHERE id = %s", (row["id"],))
        raise LifecycleError("Optiwar did not accept it - nothing was recorded (%s)"
                             % ((row or {}).get("last_error") or "no response"))
    return row


def after_sync(row):
    """Fold Optiwar's answer to an rp_* sync into the projection."""
    if row and row["state"] == "synced" and row.get("response") and row["kind"] in ALL_SYNC_KINDS:
        try:
            data = json.loads(row["response"])
        except (TypeError, ValueError):
            data = None
        _apply_optiwar_body(row["ow_order_id"], data, "sync %s #%s" % (row["kind"], row["id"]))
    return row


def pending_for(pickup_id, order_id=None):
    with db_cursor() as cur:
        if pickup_id:
            cur.execute("SELECT * FROM ow_reship_sync WHERE local_ref_id = %s "
                        "AND state NOT IN ('synced', 'refused') "
                        "AND kind IN (" + ",".join(["%s"] * len(ALL_SYNC_KINDS)) + ") ORDER BY id",
                        (pickup_id,) + ALL_SYNC_KINDS)
        else:
            cur.execute("SELECT * FROM ow_reship_sync WHERE ow_order_id = %s AND local_ref_id IS NULL "
                        "AND state NOT IN ('synced', 'refused') AND kind = 'rp_waive' ORDER BY id",
                        (order_id,))
        return cur.fetchall()


def request_waiver(order_id, pickup_id, code, note, user):
    _require_live("waive")
    codes = dict(WAIVER_CODES)
    if code not in codes:
        raise LifecycleError("Pick a waiver reason code")
    note = (note or "").strip()
    if code == "OTHER" and not note:
        raise LifecycleError("Reason OTHER needs an explanation")
    fv = fee_view(order_id)
    if fv["state"] in ("PAID", "REFUNDED", "PARTIALLY_REFUNDED"):
        raise LifecycleError("Optiwar shows the fee as %s - it cannot be waived" % fv["state"])
    if fv["state"] == "WAIVED":
        raise LifecycleError("Optiwar already shows the fee as WAIVED")
    with db_cursor() as cur:
        cur.execute("""SELECT s.id FROM ow_reship_sync s WHERE s.ow_order_id = %s
                       AND s.kind = 'rp_waive' ORDER BY s.id DESC LIMIT 1""", (order_id,))
        prior = cur.fetchone()
    if prior:
        raise LifecycleError("A waiver was already sent to Optiwar for this order - "
                             "use Retry Sync if it is pending")
    p = _pickup(pickup_id) if pickup_id else None
    with db_cursor() as cur:
        cur.execute("""INSERT INTO ow_rp_waivers (ow_order_id, pickup_id, reason_code, note, created_by)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (order_id, pickup_id, code, note[:500], (user or {}).get("id")))
    _event(order_id, pickup_id, "fee_waiver_requested", {"reason_code": code, "note": note}, user)
    payload = {"operator": reship._operator(user), "reason_code": code, "note": note}
    if p:
        payload["awb"] = p["waybill"]
    return _sync(order_id, "rp_waive", payload, user, pickup_id)


def correct_reason(pickup_id, reason, user):
    _require_live("reason")
    p = _pickup(pickup_id)
    if not p or p["status"] == "cancelled":
        raise LifecycleError("Reverse pickup not found or cancelled")
    reason = (reason or "").strip()
    if not reason:
        raise LifecycleError("Pick a reason")
    if reason == p["reason"]:
        raise LifecycleError("Reason is already %s" % reason)
    with db_cursor() as cur:
        cur.execute("UPDATE ow_reverse_pickups SET reason = %s WHERE id = %s", (reason[:120], pickup_id))
    _event(p["ow_order_id"], pickup_id, "reason_corrected", {"from": p["reason"], "to": reason}, user)
    return _sync(p["ow_order_id"], "rp_reason",
                 {"operator": reship._operator(user), "awb": p["waybill"], "reason": reason},
                 user, pickup_id)


def _require(p, life, *, received=None, inspected=None):
    if not p or p["status"] == "cancelled":
        raise LifecycleError("Reverse pickup not found or cancelled")
    if received is True and not life.get("received_at"):
        raise LifecycleError("Record the parcel as received first")
    if received is False and life.get("received_at"):
        raise LifecycleError("Parcel already recorded as received %s" % life["received_at"])
    if inspected is True and life.get("defect") is None:
        raise LifecycleError("Inspect the return first")
    if inspected is False and life.get("defect") is not None:
        raise LifecycleError("Inspection already recorded %s" % life.get("inspected_at"))


def received_block(pickup_id):
    """Why a pickup may no longer be cancelled ('' when it may)."""
    p = _pickup(pickup_id)
    life = life_for(pickup_id)
    if life.get("received_at"):
        return "Parcel already recorded as received %s - a received pickup cannot be cancelled" \
            % life["received_at"]
    if p and is_live("state"):
        refresh_state(p["ow_order_id"])
        got = fee_for(p["ow_order_id"]).get("ow_received_at")
        if got:
            return "Optiwar has the parcel as received %s - cancel refused" % got
    return ""


def mark_received(pickup_id, condition, notes, user):
    _require_live("received")
    p = _pickup(pickup_id)
    life = life_for(pickup_id)
    _require(p, life, received=False)
    if condition not in CONDITIONS:
        raise LifecycleError("Pick the parcel condition")
    _ensure_life(p)
    with db_cursor() as cur:
        cur.execute("""UPDATE ow_rp_lifecycle SET received_at = NOW(), received_by = %s,
                       parcel_condition = %s, received_notes = %s
                       WHERE pickup_id = %s AND received_at IS NULL""",
                    ((user or {}).get("id"), condition, (notes or "").strip()[:500], pickup_id))
    life = life_for(pickup_id)
    _event(p["ow_order_id"], pickup_id, "received",
           {"awb": p["waybill"], "condition": condition, "notes": notes}, user)
    return _sync(p["ow_order_id"], "rp_received",
                 {"operator": reship._operator(user), "awb": p["waybill"],
                  "received_at": str(life["received_at"]), "condition": condition,
                  "notes": (notes or "").strip()}, user, pickup_id)


def save_evidence(p, filename, content, user):
    name = os.path.basename(filename or "").replace(" ", "_")[:150]
    if not name or not content:
        return None
    if not name.lower().endswith(EVIDENCE_EXT):
        raise LifecycleError("Evidence %s: only %s files" % (name, ", ".join(EVIDENCE_EXT)))
    if len(content) > EVIDENCE_MAX_BYTES:
        raise LifecycleError("Evidence %s is larger than 15 MB" % name)
    folder = EVIDENCE_DIR / p["ow_order_id"].replace("/", "_")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("%d_%d_%s" % (p["id"], int(time.time() * 1000), name))
    path.write_bytes(content)
    with db_cursor() as cur:
        cur.execute("""INSERT INTO ow_rp_evidence (pickup_id, ow_order_id, filename, path, uploaded_by)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (p["id"], p["ow_order_id"], name, str(path), (user or {}).get("id")))
        return cur.lastrowid


def inspect(pickup_id, defect, remarks, files, user):
    """files: list of (filename, bytes). The defect decision is the operator's."""
    _require_live("inspection")
    p = _pickup(pickup_id)
    life = life_for(pickup_id)
    _require(p, life, received=True, inspected=False)
    if defect not in ("YES", "NO"):
        raise LifecycleError("Choose MANUFACTURING DEFECT: YES or NO")
    remarks = (remarks or "").strip()
    if not remarks:
        raise LifecycleError("Inspection remarks are required")
    for fn, content in files or []:
        if content and not os.path.basename(fn or "").lower().endswith(EVIDENCE_EXT):
            raise LifecycleError("Evidence %s: only %s files" % (fn, ", ".join(EVIDENCE_EXT)))
    saved = [save_evidence(p, fn, content, user) for fn, content in files or []]
    with db_cursor() as cur:
        cur.execute("""UPDATE ow_rp_lifecycle SET inspected_at = NOW(), inspected_by = %s,
                       defect = %s, inspection_remarks = %s
                       WHERE pickup_id = %s AND defect IS NULL""",
                    ((user or {}).get("id"), 1 if defect == "YES" else 0, remarks[:1000], pickup_id))
    life = life_for(pickup_id)
    _event(p["ow_order_id"], pickup_id, "inspection_completed",
           {"manufacturing_defect": defect, "remarks": remarks,
            "evidence": [x for x in saved if x]}, user)
    return _sync(p["ow_order_id"], "rp_inspection",
                 {"operator": reship._operator(user), "awb": p["waybill"],
                  "manufacturing_defect": defect == "YES", "remarks": remarks,
                  "inspected_at": str(life["inspected_at"])}, user, pickup_id)


def decide_request(order_id, decision, note, user):
    """Ops' decision on a customer-opened return request. Optiwar must accept
    it before anything is recorded here."""
    _require_live("decision")
    if decision not in dict(DECISIONS):
        raise LifecycleError("Choose a decision")
    note = (note or "").strip()
    if decision != "APPROVED_SUBJECT_TO_INSPECTION" and len(note) < 10:
        raise LifecycleError("A note of at least 10 characters is required for %s" % decision)
    fv = fee_view(order_id, refresh=True)
    req = fv["request"] or {}
    if req.get("source") != "customer":
        raise LifecycleError("No customer return request on record at Optiwar")
    if isinstance(req.get("decision"), dict) and req["decision"].get("status"):
        raise LifecycleError("Already decided: %s" % req["decision"]["status"])
    row = _call(order_id, "rp_decision", {"operator": reship._operator(user),
                                          "decision": decision, "note": note}, user, None)
    _event(order_id, None, "request_decided", {"decision": decision, "note": note}, user)
    _COUNT["at"] = 0.0
    return row


def forward_allowed(pickup_id):
    if not is_live("forward"):
        return False, "Optiwar forward-shipped endpoint not live yet"
    p = _pickup(pickup_id)
    if not p:
        return False, "Reverse pickup not found"
    view = case_view(p)
    if view["bucket"] != "forward" or view["life"].get("fwd_awb"):
        return False, "Forward dispatch needs READY FOR FORWARD DISPATCH (now: %s)" % view["label"]
    return True, ""


def record_forward(pickup_id, courier, awb, awb_row_id, user):
    _require_live("forward")
    p = _pickup(pickup_id)
    _ensure_life(p)
    with db_cursor() as cur:
        cur.execute("""UPDATE ow_rp_lifecycle SET fwd_courier = %s, fwd_awb = %s, fwd_awb_row_id = %s,
                       fwd_booked_by = %s, fwd_booked_at = NOW()
                       WHERE pickup_id = %s AND fwd_awb = ''""",
                    (courier, awb, awb_row_id, (user or {}).get("id"), pickup_id))
    _event(p["ow_order_id"], pickup_id, "forward_booked", {"courier": courier, "awb": awb}, user)


def shipment_types(life):
    return SHIPMENT_TYPES.get(life.get("defect"), ())


def _iso(v):
    return str(v).replace(" ", "T") if v else None


def confirm_dispatch(pickup_id, shipment_type, user):
    _require_live("forward")
    p = _pickup(pickup_id)
    life = life_for(pickup_id)
    if not life.get("fwd_awb"):
        raise LifecycleError("Book the forward shipment first")
    if life.get("fwd_dispatch_at"):
        raise LifecycleError("Already dispatched %s" % life["fwd_dispatch_at"])
    allowed = dict(shipment_types(life))
    if shipment_type not in allowed:
        raise LifecycleError("Shipment type must be one of: %s" % ", ".join(allowed))
    with db_cursor() as cur:
        cur.execute("""UPDATE ow_rp_lifecycle SET fwd_dispatch_by = %s, fwd_dispatch_at = NOW(),
                       fwd_type = %s WHERE pickup_id = %s AND fwd_dispatch_at IS NULL""",
                    ((user or {}).get("id"), shipment_type, pickup_id))
    life = life_for(pickup_id)
    _event(p["ow_order_id"], pickup_id, "forward_dispatched",
           {"shipment_type": shipment_type, "courier": life["fwd_courier"],
            "awb": life["fwd_awb"]}, user)
    return _sync(p["ow_order_id"], "rp_forward",
                 {"operator": reship._operator(user), "shipment_type": shipment_type,
                  "courier": life["fwd_courier"], "awb": life["fwd_awb"],
                  "shipped_at": _iso(life["fwd_dispatch_at"])}, user, pickup_id)


def complete(pickup_id, outcome, note, user):
    """outcome 'shipped' (from the forward shipment type) or PRODUCT_REFUNDED.
    Optiwar must accept the closure before it is recorded here."""
    _require_live("complete")
    p = _pickup(pickup_id)
    life = life_for(pickup_id)
    if life.get("completed_at"):
        raise LifecycleError("Case already complete %s" % life["completed_at"])
    note = (note or "").strip()
    if outcome == "PRODUCT_REFUNDED":
        _require(p, life, received=True, inspected=True)
        if life.get("defect") != 1:
            raise LifecycleError("PRODUCT_REFUNDED needs a confirmed manufacturing defect")
        if life.get("fwd_awb"):
            raise LifecycleError("A forward shipment exists - complete it as shipped")
    elif outcome == "shipped":
        if not life.get("fwd_dispatch_at"):
            raise LifecycleError("Forward shipment not dispatched yet")
        outcome = OUTCOME_FOR_TYPE.get(life.get("fwd_type") or "")
        if not outcome:
            raise LifecycleError("Forward shipment type unknown")
    else:
        raise LifecycleError("Unknown outcome")
    row = _call(p["ow_order_id"], "rp_complete",
                {"operator": reship._operator(user), "outcome": outcome, "note": note},
                user, pickup_id)
    with db_cursor() as cur:
        cur.execute("""UPDATE ow_rp_lifecycle SET completed_at = NOW(), completed_by = %s,
                       outcome = %s, completion_note = %s
                       WHERE pickup_id = %s AND completed_at IS NULL""",
                    ((user or {}).get("id"), outcome, note[:500], pickup_id))
    _event(p["ow_order_id"], pickup_id, "completed", {"outcome": outcome, "note": note}, user)
    _COUNT["at"] = 0.0
    return row


# --------------------------------------------------------------------------
# Courier tracking (cached) and deterministic queue state
# --------------------------------------------------------------------------

_TRACK_CACHE = {}
TRACK_TTL = 600


def _delhivery_status(waybill):
    key = ("D", waybill)
    hit = _TRACK_CACHE.get(key)
    if hit and time.time() - hit[0] < TRACK_TTL:
        return hit[1]
    try:
        st = delhivery.track_status(waybill)
    except (delhivery.DelhiveryError, requests.RequestException, ValueError) as e:
        st = {"error": str(e)[:120]}
    _TRACK_CACHE[key] = (time.time(), st)
    return st


def _forward_delivered(courier, awb):
    if courier == "Delhivery":
        st = _delhivery_status(awb)
        return st.get("type") == "DL" or str(st.get("status") or "").lower() == "delivered", st
    key = ("T", awb)
    hit = _TRACK_CACHE.get(key)
    if hit and time.time() - hit[0] < TRACK_TTL:
        return hit[1]
    try:
        data = dtdc.track(awb)
        head = (data.get("trackHeader") or {}) if isinstance(data, dict) else {}
        text = str(head.get("strStatus") or "")
        res = (text.strip().lower() == "delivered", {"status": text})
    except (dtdc.DtdcError, requests.RequestException, ValueError) as e:
        res = (False, {"error": str(e)[:120]})
    _TRACK_CACHE[key] = (time.time(), res)
    return res


def case_view(p, track=False):
    """Bucket and facts for one booked reverse pickup."""
    life = life_for(p["id"])
    fv = fee_view(p["ow_order_id"])
    pend = pending_for(p["id"])
    tracking = {}
    note = ""
    if p["status"] == "cancelled":
        bucket = None
    elif pend:
        bucket, note = "exception", "Optiwar sync pending (%s): %s" % (pend[0]["kind"],
                                                                     pend[0]["last_error"] or "")
    elif life.get("completed_at") or fv["completion"] or fv["queue_state"] == "COMPLETED":
        bucket = "complete"
    elif fv["queue_state"] == "ABANDONED" or (fv["hold"] or {}).get("abandoned"):
        bucket, note = "abandoned", "Optiwar closed the case: no customer reply within the hold"
    elif life.get("fwd_dispatch_at"):
        bucket = "shipped"
        if track:
            delivered, tracking = _forward_delivered(life["fwd_courier"], life["fwd_awb"])
            if delivered:
                note = "Courier reports DELIVERED — complete the case"
    elif life.get("defect") == 1:
        if fv["state"] == "PAID":
            bucket = "refund"
            last = fv["refunds"][-1] if fv["refunds"] else {}
            rstate = str(last.get("state") or fv["refund_state"] or "").upper()
            if rstate in ("FAILED", "EXCEPTION") or fv["queue_state"] == "REFUND_EXCEPTION":
                bucket, note = "exception", "Optiwar's ₹250 refund %s%s — check by hand with Optiwar" % (
                    rstate or "EXCEPTION", (": " + str(last["error"])[:120]) if last.get("error") else "")
            else:
                note = ("Optiwar refunds the ₹250 itself (%s) — no Ops action; dispatch opens "
                        "once it is REFUNDED" % (rstate or "REFUND PENDING"))
        elif fv["state"] in ("WAIVED", "REFUNDED", "PARTIALLY_REFUNDED"):
            bucket = "forward"
        else:
            bucket, note = "exception", "Defect confirmed but Optiwar has not reported the fee state"
    elif life.get("defect") == 0:
        bucket = "forward" if fv["consent_at"] else "consent"
        hold = fv["hold"] or {}
        if bucket == "consent" and hold.get("days_remaining") is not None:
            note = "%sOptiwar holds the parcel: %s day(s) left (abandon at %s)" % (
                "ABANDONMENT PENDING — " if hold.get("final_period") else "",
                hold["days_remaining"], hold.get("abandon_at") or "?")
    elif life.get("received_at"):
        bucket = "inspect"
    else:
        bucket = "booked"
        if track:
            tracking = _delhivery_status(p["waybill"])
            t = tracking.get("type")
            if t and t != "PP":
                bucket = "in_transit"
            if t == "CN" or str(tracking.get("status") or "").lower() == "cancelled":
                bucket, note = "exception", "Delhivery shows the pickup cancelled"
            elif t in ("DL",) or str(tracking.get("status") or "").upper() == "DTO":
                note = "Delhivery reports delivered to warehouse — confirm receipt"
    step = {"inspect": "inspection", "forward": "forward", "shipped": "complete"}.get(bucket)
    if step and not is_live(step) and not note:
        note = "Optiwar's %s endpoint is not live yet - this step waits for it" % step
    return {"pickup": p, "life": life, "fee": fv, "bucket": bucket,
            "label": BUCKET_LABEL.get(bucket, "CANCELLED"), "note": note,
            "pending": pend, "tracking": tracking, "evidence": evidence_for(p["id"])}


def _optiwar_requests():
    if not is_live("queue"):
        return [], "Optiwar return-request queue not live yet"
    try:
        data = _get(QUEUE_PATH)
    except (LifecycleError, requests.RequestException) as e:
        return [], str(e)[:200]
    out = []
    for r in data.get("requests") or []:
        if not isinstance(r, dict) or not r.get("order_id"):
            continue
        oid = str(r["order_id"]).upper().replace("OW-", "", 1)
        _apply_optiwar_body(oid, {"fee": r.get("fee"), "queue_state": r.get("queue_state"),
                                  "label": r.get("label"), "request": {
            "id": r.get("request_id"), "declared_reason": r.get("declared_reason")}}, "GET queue")
        out.append(dict(r, order_id=oid))
    return out, ""


def board(track=True):
    buckets = {k: [] for k, _ in BUCKETS}
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_reverse_pickups WHERE status <> 'cancelled' ORDER BY id DESC")
        pickups = cur.fetchall()
    booked_orders = set()
    for p in pickups:
        v = case_view(p, track=track)
        booked_orders.add(p["ow_order_id"])
        if v["bucket"]:
            buckets[v["bucket"]].append(v)
    reqs, err = _optiwar_requests()
    for r in reqs:
        if r["order_id"] in booked_orders or r.get("pickup_awb"):
            continue
        fv = fee_view(r["order_id"])
        qs = str(r.get("queue_state") or "").upper()
        if qs in REQUEST_GONE:
            continue
        if qs:
            key = REQUEST_BUCKET.get(qs, "exception")
            note = "" if key != "exception" else "Unexpected Optiwar state %s" % qs
            if qs == "INFO_REQUESTED":
                note = "Waiting for the customer's further information"
        else:
            key = "ready_to_book" if fv["bookable"] else "awaiting_fee" if fv["state"] == "DUE" \
                else "exception"
            note = "" if key != "exception" else "Unexpected fee state %s" % (fv["state"] or "none")
        buckets[key].append({"request": r, "fee": fv, "bucket": key,
                             "label": BUCKET_LABEL[key], "note": note})
    return {"buckets": buckets, "queue_error": err}


_COUNT = {"at": 0.0, "n": 0}


def actionable_count():
    """Cases waiting on an Ops action (DB only, cached for a minute)."""
    if time.time() - _COUNT["at"] < 60:
        return _COUNT["n"]
    n = 0
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_reverse_pickups WHERE status <> 'cancelled'")
        for p in cur.fetchall():
            if case_view(p)["bucket"] in ("inspect", "forward", "shipped", "exception"):
                n += 1
    _COUNT.update(at=time.time(), n=n)
    return n


def timeline(order_id, pickup, life, fee):
    """Full return history in order: original dispatch, fee, pickup, receipt,
    inspection, refund/consent, forward dispatch, completion."""
    rows = []
    with ow_cursor() as cur:
        cur.execute("""SELECT courier, tracking_number, created_at FROM ops_shipping_awb
                       WHERE ow_order_id = %s ORDER BY id LIMIT 1""", (order_id,))
        first = cur.fetchone()
    if first:
        rows.append((first["created_at"], "Original dispatch",
                     "%s %s" % (first["courier"], first["tracking_number"]), "ops"))
    rows.append((pickup["created_at"], "Reverse pickup booked",
                 "Delhivery %s (%s)" % (pickup["waybill"], pickup["reference"]), "ops"))
    for e in events_for(order_id):
        if e["pickup_id"] not in (None, pickup["id"]):
            continue
        rows.append((e["at"], e["event"].replace("_", " "),
                     ", ".join("%s: %s" % (k, v) for k, v in sorted(e["data"].items())
                               if v not in (None, "", []) and k not in ("event_id", "order_ref")),
                     "%s%s" % (e["source"], (" · " + e["actor_name"]) if e.get("actor_name") else "")))
    rows.sort(key=lambda r: str(r[0]))
    return rows


# --------------------------------------------------------------------------
# Signed reverse_pickup.* webhooks
# --------------------------------------------------------------------------

def apply_webhook(payload):
    """Fold one verified, stored reverse_pickup.* event into the projection
    and the order's event log. Safe to re-run for the same event."""
    event = str(payload.get("event") or "")
    oid = str(payload.get("order_ref") or payload.get("order_id") or "").strip().upper()
    if oid.startswith("OW-"):
        oid = oid[3:]
    if not oid:
        raise ValueError("order_ref required")
    eid = str(payload.get("event_id") or "")
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM ow_rp_events WHERE ow_order_id = %s AND source = 'optiwar' "
                    "AND JSON_UNQUOTE(JSON_EXTRACT(data, '$.event_id')) = %s", (oid, eid))
        if cur.fetchone():
            return
    src = "webhook %s" % event
    if isinstance(payload.get("fee"), dict):
        apply_fee(oid, payload["fee"], src)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    short = event.split(".", 1)[-1]
    if short == "customer_consent_received":
        _set_fee_fields(oid, src, consent_at=str(data.get("consent_at") or data.get("at")
                                                  or payload.get("at") or payload.get("occurred_at")
                                                  or "")[:19] or None,
                        consent_by=str(data.get("recorded_by") or "")[:191])
    elif short == "fee_refunded":
        _set_fee_fields(oid, src, refund_id=str(data.get("refund_id") or "")[:80],
                        refund_state=str(data.get("status") or data.get("state")
                                         or "REFUNDED").upper()[:24],
                        razorpay_refund_id=str(data.get("razorpay_refund_id")
                                               or data.get("refund_id") or "")[:60])
    elif short == "fee_refund_failed":
        _set_fee_fields(oid, src, refund_state="FAILED", queue_state="REFUND_EXCEPTION")
    elif short == "request_decided":
        _merge_json(oid, "request_json", src, {"decision": {
            "status": data.get("decision"), "note": data.get("note"),
            "decided_by": data.get("decided_by"), "decided_at": data.get("decided_at")}})
    elif short == "forward_shipped":
        _set_fee_fields(oid, src, forward_json=json.dumps({
            "type": data.get("shipment_type"), "courier": data.get("courier"),
            "awb": data.get("awb"), "shipped_by": data.get("shipped_by"),
            "shipped_at": data.get("shipped_at")}), queue_state="FORWARD_SHIPPED")
    elif short == "completed":
        _set_fee_fields(oid, src, completion_json=json.dumps({
            "outcome": data.get("outcome"), "note": data.get("note"),
            "completed_by": data.get("completed_by"), "completed_at": data.get("completed_at")}),
            queue_state="COMPLETED")
    elif short == "abandoned":
        _merge_json(oid, "hold_json", src, {"abandoned": True, "active": False,
                                            "abandoned_at": data.get("abandoned_at"),
                                            "abandon_reason": data.get("reason")})
        _set_fee_fields(oid, src, queue_state="ABANDONED")
    elif short in ("hold_reminder", "hold_final_warning"):
        _merge_json(oid, "hold_json", src, {"days_remaining": data.get("days_remaining"),
                                            "abandon_at": data.get("abandon_at"),
                                            "final_period": short == "hold_final_warning" or None})
    elif short == "fee_paid" and data.get("razorpay_payment_id"):
        _set_fee_fields(oid, src, razorpay_payment_id=str(data["razorpay_payment_id"])[:60])
    elif short == "requested":
        _set_fee_fields(oid, src, request_id=str(payload.get("request_id")
                                                 or payload.get("case_id") or "")[:40],
                        declared_reason=str(data.get("declared_reason") or data.get("reason")
                                            or "")[:255], queue_state="REQUESTED")
        _merge_json(oid, "request_json", src, {
            "id": payload.get("request_id") or payload.get("case_id"),
            "source": data.get("source") or "customer", "reason_code": data.get("reason_code"),
            "reason": data.get("reason"), "description": data.get("description"),
            "requested_at": data.get("requested_at"), "photo_count": data.get("photo_count")})
    puuid = payload.get("pickup_uuid") or payload.get("reverse_pickup_id")
    if puuid:
        _set_fee_fields(oid, src, optiwar_pickup_uuid=str(puuid)[:40])
    pid = None
    awb = str(payload.get("awb") or "").strip()
    if awb:
        with db_cursor() as cur:
            cur.execute("SELECT id FROM ow_reverse_pickups WHERE waybill = %s", (awb,))
            r = cur.fetchone()
            pid = r["id"] if r else None
    shown = dict(data, event_id=eid)
    if isinstance(payload.get("fee"), dict) and payload["fee"].get("state"):
        shown["fee"] = payload["fee"]["state"]
    _event(oid, pid, "optiwar: " + short, shown, source="optiwar")
    _COUNT["at"] = 0.0


def record_booked(row, fee_state, user):
    _ensure_life(row)
    _event(row["ow_order_id"], row["id"], "pickup_booked",
           {"awb": row["waybill"], "reference": row["reference"], "reason": row["reason"],
            "fee_at_booking": fee_state}, user)
    _COUNT["at"] = 0.0
