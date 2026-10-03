"""India reverse pickup: Ops books a Delhivery reverse waybill, Optiwar
records it against the order and tells the customer.

Optiwar is the authority for the ₹250 reverse-pickup fee: one
``reverse_pickup_cases`` row per return holds its state (DUE / PAID / WAIVED /
REFUNDED / PARTIALLY_REFUNDED), and a waybill is recorded only once that fee is
PAID or WAIVED. Every transition Ops must know about is written to
``reverse_pickup_ops_outbox`` in the same transaction and delivered, signed,
until Ops answers 2xx.

Ops owns the courier booking; this module owns the customer's side of it:
one ``order_reverse_pickups`` row per waybill, an order-history line, the
email / WhatsApp notice (once per channel, claimed in ``reship_events``),
and the My Orders card. The forward AWB is never rewritten, the customer
tracking link is built here from the AWB (never taken from the request), and
a replay of the same AWB returns the stored row without a second message.
"""
import datetime
import hashlib
import hmac
import json
import os
import uuid

try:
    from . import reship
    from .paid_orders import add_history
    from .policy_terms import TERMS_URL
except ImportError:  # pragma: no cover - flat import in scripts
    import reship
    from paid_orders import add_history
    from policy_terms import TERMS_URL

ENABLED_ENV = "REVERSE_PICKUP_ENABLED"
WA_APPROVED_ENV = "REVERSE_PICKUP_WA_TEMPLATES_APPROVED"
# Off: Ops can book and cancel, but the customer is not emailed, messaged or
# shown the pickup in My Orders.
CUSTOMER_ENV = "REVERSE_PICKUP_CUSTOMER_ENABLED"

COURIER = "Delhivery"

ST_BOOKED = "BOOKED"
ST_CANCELLED = "CANCELLED"

EV_BOOKED = "reverse_pickup.booked"
EV_CANCELLED = "reverse_pickup.cancelled"
EV_NOTIFIED = "reverse_pickup.notified"
EV_NOTIFY_FAILED = "reverse_pickup.notification_failed"
EV_FEE_WAIVED = "reverse_pickup.fee_waived"
EV_RECEIVED = "reverse_pickup.received"
EV_INSPECTED = "reverse_pickup.inspection_completed"
EV_CONSENT = "reverse_pickup.customer_consent_received"
# The order-history / audit name of a customer's emailed consent.
CONSENT_RECORD = "CUSTOMER_RETURN_CONSENT_RECEIVED"
# Internal audit only; not an Ops event.
EV_REASON_CORRECTED = "reverse_pickup.reason_corrected"
EV_REQUESTED = "reverse_pickup.requested"
EV_REQUEST_DECIDED = "reverse_pickup.request_decided"

FEE_DUE = "DUE"
FEE_PAID = "PAID"
FEE_WAIVED = "WAIVED"
FEE_REFUNDED = "REFUNDED"
FEE_PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
FEE_SETTLED = (FEE_PAID, FEE_WAIVED)
FEE_MINOR = reship.FEE_MINOR
FEE_CURRENCY = reship.CURRENCY

WAIVER_REASONS = ("OWNER_DECISION", "GOODWILL", "DEFECT_EVIDENT_PRE_PICKUP",
                  "PRE_EXISTING_CASE", "BOOKED_BEFORE_FEE_FLOW", "OTHER")

RECEIVED_CONDITIONS = ("Intact", "Damaged packaging", "Product damaged", "Wrong item",
                       "Empty/missing")

NOTICE_INSPECTION_NO_DEFECT = "inspection_no_defect"
NOTICE_INSPECTION_DEFECT_WAIVED = "inspection_defect_waived"
NOTICE_REQUEST_RECEIVED = "request_received"
NOTICE_REQUEST_APPROVED = "request_approved"
NOTICE_REQUEST_APPROVED_WAIVED = "request_approved_fee_waived"
NOTICE_REQUEST_INFO = "request_information_needed"
NOTICE_REQUEST_DECLINED = "request_not_approved"

# A customer's return request. Ops-created cases have no request status; a
# customer-created case is booked only once Ops approved it.
REQ_SUBMITTED = "SUBMITTED"
REQ_APPROVED = "APPROVED_SUBJECT_TO_INSPECTION"
REQ_INFO = "FURTHER_INFORMATION_REQUIRED"
REQ_DECLINED = "NOT_APPROVED"
REQ_OPEN = (REQ_SUBMITTED, REQ_INFO)
OUTCOME_NOT_APPROVED = "REQUEST_NOT_APPROVED"
MY_ORDERS_URL_IN = "https://optiwar.in/profile/?tab=orders"

QUEUE_LABELS = {"AWAITING_FEE": "AWAITING ₹250",
                "AWAITING_INSPECTION": "RECEIVED — AWAITING INSPECTION",
                "AWAITING_CUSTOMER_CONSENT": "AWAITING CUSTOMER CONSENT",
                "DEFECT_CONFIRMED": "DEFECT CONFIRMED",
                "READY_TO_DISPATCH": "READY TO DISPATCH",
                "COMPLETED": "COMPLETED",
                "READY_TO_BOOK": "READY TO BOOK PICKUP",
                "PICKUP_BOOKED": "PICKUP BOOKED",
                "FEE_NOT_RECORDED": "FEE NOT RECORDED",
                "REQUESTED": "RETURN REQUESTED — AWAITING REVIEW",
                "INFO_REQUESTED": "FURTHER INFORMATION REQUESTED",
                "NOT_APPROVED": "RETURN NOT APPROVED"}

OUT_PENDING = "PENDING"
OUT_SENT = "SENT"
OUT_FAILED = "FAILED"

TABLE_DDL = """CREATE TABLE IF NOT EXISTS order_reverse_pickups (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    pickup_uuid     CHAR(36) NOT NULL,
    order_id        VARCHAR(64) NOT NULL,
    customer_id     BIGINT NULL,
    site_from       VARCHAR(64) NULL,
    courier         VARCHAR(64) NOT NULL,
    awb             VARCHAR(32) NOT NULL,
    reference       VARCHAR(64) NULL,
    reason          VARCHAR(255) NULL,
    remarks         VARCHAR(500) NULL,
    forward_awb     VARCHAR(64) NULL,
    pickup_city     VARCHAR(64) NULL,
    pickup_pin      VARCHAR(12) NULL,
    status          VARCHAR(16) NOT NULL,
    booked_by       VARCHAR(191) NOT NULL,
    booked_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    cancelled_by    VARCHAR(191) NULL,
    cancelled_at    DATETIME NULL,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_rp_uuid (pickup_uuid),
    UNIQUE KEY uq_rp_awb (awb),
    KEY idx_rp_order (order_id, status),
    KEY idx_rp_customer (customer_id, booked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

CASE_DDL = """CREATE TABLE IF NOT EXISTS reverse_pickup_cases (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    case_uuid           CHAR(36) NOT NULL,
    order_id            VARCHAR(64) NOT NULL,
    case_no             SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    customer_id         BIGINT NULL,
    site_from           VARCHAR(64) NULL,
    source              VARCHAR(24) NOT NULL,
    return_reason       VARCHAR(255) NULL,
    fee_state           VARCHAR(24) NOT NULL,
    fee_amount_minor    INT NOT NULL,
    fee_currency        CHAR(3) NOT NULL,
    fee_refunded_minor  INT NOT NULL DEFAULT 0,
    fee_paid_at         DATETIME NULL,
    razorpay_order_id   VARCHAR(64) NULL,
    razorpay_payment_id VARCHAR(64) NULL,
    waiver_reason_code  VARCHAR(32) NULL,
    waiver_note         VARCHAR(500) NULL,
    waived_by           VARCHAR(191) NULL,
    waived_at           DATETIME NULL,
    received_by         VARCHAR(191) NULL,
    received_at         DATETIME NULL,
    received_awb        VARCHAR(32) NULL,
    received_condition  VARCHAR(32) NULL,
    received_notes      VARCHAR(500) NULL,
    inspection_defect   TINYINT(1) NULL,
    inspection_remarks  VARCHAR(1000) NULL,
    inspected_by        VARCHAR(191) NULL,
    inspected_at        DATETIME NULL,
    consent_recorded_by VARCHAR(191) NULL,
    consent_at          DATETIME NULL,
    consent_message_id  VARCHAR(255) NULL,
    completed_outcome   VARCHAR(32) NULL,
    completed_note      VARCHAR(500) NULL,
    completed_by        VARCHAR(191) NULL,
    completed_at        DATETIME NULL,
    created_by          VARCHAR(191) NOT NULL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_rpc_uuid (case_uuid),
    UNIQUE KEY uq_rpc_order_no (order_id, case_no),
    UNIQUE KEY uq_rpc_payment (razorpay_payment_id),
    KEY idx_rpc_fee (fee_state, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

OUTBOX_DDL = """CREATE TABLE IF NOT EXISTS reverse_pickup_ops_outbox (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    event_id    CHAR(36) NOT NULL,
    event       VARCHAR(64) NOT NULL,
    order_id    VARCHAR(64) NOT NULL,
    case_uuid   CHAR(36) NULL,
    body        MEDIUMTEXT NOT NULL,
    status      VARCHAR(12) NOT NULL,
    attempts    INT NOT NULL DEFAULT 0,
    last_status INT NULL,
    last_error  VARCHAR(255) NULL,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at     DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_rpo_event (event_id),
    KEY idx_rpo_status (status, id),
    KEY idx_rpo_order (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

# One row per customer notice (case, notice, channel): the step that causes it
# is recorded once; the notice is retried on its own until it is sent.
NOTICE_DDL = """CREATE TABLE IF NOT EXISTS reverse_pickup_notifications (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    event_id          CHAR(36) NOT NULL,
    case_uuid         CHAR(36) NOT NULL,
    order_id          VARCHAR(64) NOT NULL,
    notification_type VARCHAR(64) NOT NULL,
    channel           VARCHAR(16) NOT NULL,
    status            VARCHAR(12) NOT NULL,
    attempt_count     INT NOT NULL DEFAULT 0,
    last_attempt_at   DATETIME NULL,
    last_error        VARCHAR(255) NULL,
    sent_at           DATETIME NULL,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_rpn_event (event_id),
    UNIQUE KEY uq_rpn_notice (case_uuid, notification_type, channel),
    KEY idx_rpn_status (status, last_attempt_at),
    KEY idx_rpn_order (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

CASE_ADDED_COLUMNS = (
    ("request_status", "VARCHAR(40) NULL"),
    ("reason_code", "VARCHAR(32) NULL"),
    ("defect_description", "VARCHAR(2000) NULL"),
    ("declarations_version", "VARCHAR(40) NULL"),
    ("declarations_sha256", "CHAR(64) NULL"),
    ("declarations_accepted_at", "DATETIME NULL"),
    ("declarations_ip", "VARCHAR(64) NULL"),
    ("decision_note", "VARCHAR(1000) NULL"),
    ("decided_by", "VARCHAR(191) NULL"),
    ("decided_at", "DATETIME NULL"),
)

TABLES = [("order_reverse_pickups", TABLE_DDL), ("reverse_pickup_cases", CASE_DDL),
          ("reverse_pickup_ops_outbox", OUTBOX_DDL), ("reverse_pickup_notifications", NOTICE_DDL)]

NOTICE_PENDING = "PENDING"
NOTICE_SENDING = "SENDING"
NOTICE_SENT = "SENT"
NOTICE_FAILED = "FAILED"
NOTICE_NO_EMAIL = "NO_EMAIL"
NOTICE_RETRY_MINUTES_ENV = "REVERSE_PICKUP_NOTICE_RETRY_MINUTES"
NOTICE_RETRY_MINUTES = 15

_WA_TEMPLATES = {EV_BOOKED: "reverse_pickup_booked_v2", EV_CANCELLED: "reverse_pickup_cancelled_v2"}
CHANGE_EMAIL = "admin@optiwar.com"

EMAILS = {
    EV_BOOKED: (
        "Reverse pickup scheduled for your Optiwar order {order_id}",
        "Dear {name},\n\n"
        "A reverse pickup has been scheduled for your order {order_id}.\n\n"
        "Courier: Delhivery\n"
        "Pickup AWB: {awb}\n"
        "Track: {track_url}\n\n"
        "A Delhivery agent will collect the parcel from your order address in 1-3 working days.\n\n"
        "Before the pickup:\n"
        "- Keep the product in its original box/case with all accessories and the invoice, packed and ready.\n"
        "- Keep your phone reachable so the agent can contact you.\n"
        "- Please do not ship the product yourself and do not hand it to any other courier.\n\n"
        "To change the pickup address or reschedule, reply to this email or write to {change_email}.\n\n"
        "Any reverse-pickup charge applies as described in clause 9B of our Terms "
        "(Return / Reverse-pickup cost): {terms_url}\n\n"
        "You can see this pickup in My Orders: {url}\n\n"
        "Optiwar Support"),
    EV_CANCELLED: (
        "Reverse pickup cancelled for your Optiwar order {order_id}",
        "Dear {name},\n\n"
        "The Delhivery reverse pickup for your order {order_id} (AWB {awb}) has been cancelled. "
        "No agent will come to collect the parcel for this booking.\n\n"
        "If you did not expect this, reply to this email or write to {support}.\n\n"
        "My Orders: {url}\n\n"
        "Optiwar Support"),
}

# Customer notices sent once per return case (email only). The body lines are
# the owner-approved copy, verbatim.
CASE_EMAILS = {
    EV_RECEIVED: (
        "Optiwar Return Received",
        "We have received your returned package at Optiwar.\n"
        "Our team will now inspect the product and update you after the inspection is completed.\n"
        "No further action is required from you at this stage."),
    NOTICE_INSPECTION_NO_DEFECT: (
        "Optiwar Return Inspection Update",
        "We have completed the inspection of your returned product.\n"
        "Our inspection did not confirm the manufacturing defect reported in the return request.\n"
        "{fee_line}"
        "Please reply to this email to confirm that you would like us to send the product back to you.\n"
        "Your complete return and inspection history remains recorded against your order."),
    NOTICE_INSPECTION_DEFECT_WAIVED: (
        "Optiwar Return Inspection Update",
        "We have completed the inspection of your returned product and confirmed the reported "
        "manufacturing defect.\n"
        "Your reverse-pickup fee had already been waived, so no fee refund is required.\n"
        "We will now proceed with the applicable product-resolution / return-to-customer process "
        "and update you with the next shipment details."),
}
CASE_EMAILS.update({
    NOTICE_REQUEST_RECEIVED: (
        "Optiwar Return Request Received",
        "We have received your return request for order {order_id} (reason: {reason}).\n"
        "Our team will review it and reply within 2 working days. Please do not send the product "
        "to us until we confirm the next step."),
    NOTICE_REQUEST_APPROVED: (
        "Optiwar Return Request Approved",
        "Your return request has been approved, subject to inspection.\n"
        "To arrange the Delhivery reverse pickup, please pay the ₹250 reverse-pickup fee in "
        "My Orders: {url}\n"
        "The fee is refunded if our inspection confirms a manufacturing defect."),
    NOTICE_REQUEST_APPROVED_WAIVED: (
        "Optiwar Return Request Approved",
        "Your return request has been approved, subject to inspection.\n"
        "The ₹250 reverse-pickup fee has been waived for this return, so nothing is due from you.\n"
        "We will book the Delhivery reverse pickup and send you the pickup details."),
    NOTICE_REQUEST_INFO: (
        "Optiwar Return Request: Information Needed",
        "We need a little more information before we can approve your return request:\n"
        "{note}\n"
        "Please reply to this email with the details or photos."),
    NOTICE_REQUEST_DECLINED: (
        "Optiwar Return Request Update",
        "After reviewing your return request, we are unable to approve a return for this order.\n"
        "{note}\n"
        "No fee has been charged. If you have any questions, please reply to this email."),
})
FEE_RETAINED_LINE = "The ₹250 reverse-pickup fee therefore remains applicable.\n"
CASE_EMAIL_FRAME = "Dear {name},\n\n%s\n\nOrder: {order_id}\n\nOptiwar Support"


class ReversePickupError(Exception):
    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code = code
        self.status = status


_SCHEMA_READY = False


def ensure_schema(db):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    reship.ensure_schema(db)
    cur = db.cursor()
    for _name, ddl in TABLES:
        cur.execute(ddl)
    cur.execute("SELECT column_name AS column_name FROM information_schema.columns "
                "WHERE table_schema=DATABASE() AND table_name='reverse_pickup_cases'")
    have = {r["column_name"].lower() for r in cur.fetchall()}
    for name, decl in CASE_ADDED_COLUMNS:
        if name not in have:
            cur.execute("ALTER TABLE reverse_pickup_cases ADD COLUMN %s %s" % (name, decl))
    db.commit()
    _SCHEMA_READY = True


def enabled(environ=None):
    env = os.environ if environ is None else environ
    return str(env.get(ENABLED_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def customer_enabled(environ=None):
    env = os.environ if environ is None else environ
    return str(env.get(CUSTOMER_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def _clip(value, n):
    return str(value or "").strip()[:n]


def resolve_order_id(db, order_id):
    """The order id as stored: Ops shows orders as ``OW-<id>``."""
    oid = str(order_id or "").strip()
    cur = db.cursor()
    for candidate in (oid, oid[3:] if oid.upper().startswith("OW-") else None):
        if candidate and reship._order_head(cur, candidate):
            return candidate
    return None


def by_awb(db, awb, for_update=False):
    cur = db.cursor()
    cur.execute("SELECT * FROM order_reverse_pickups WHERE awb=%s" +
                (" FOR UPDATE" if for_update else ""), (awb,))
    return cur.fetchone()


def active_for_order(db, order_id, for_update=False):
    cur = db.cursor()
    cur.execute("SELECT * FROM order_reverse_pickups WHERE order_id=%s AND status=%s "
                "ORDER BY id DESC LIMIT 1" + (" FOR UPDATE" if for_update else ""),
                (order_id, ST_BOOKED))
    return cur.fetchone()


def latest_for_customer(db, customer_id):
    """``{order_id: row}`` — each order's most recent pickup."""
    cur = db.cursor()
    cur.execute("SELECT * FROM order_reverse_pickups WHERE customer_id=%s ORDER BY id",
                (int(customer_id),))
    return {r["order_id"]: r for r in cur.fetchall()}


def _normal_awb(awb):
    return str(awb or "").strip().upper()


def book(db, order_id, body, operator):
    """Record one Ops booking. Returns ``(row, created)``; a replay of the
    same AWB for the same order returns the stored row with ``created``
    False."""
    ensure_schema(db)
    oid = resolve_order_id(db, order_id)
    if not oid:
        raise ReversePickupError("not_found", "order not found", 404)
    courier = str(body.get("courier") or "").strip()
    if courier.lower() != COURIER.lower():
        raise ReversePickupError("courier_not_supported", "only Delhivery reverse pickups are accepted")
    awb = _normal_awb(body.get("awb"))
    hint = reship.awb_format_error(COURIER, awb)
    if not awb or hint:
        raise ReversePickupError("invalid_awb", hint or "awb required")
    cur = db.cursor()
    head = reship._order_head(cur, oid)
    if not reship.is_india_host(head.get("site_from")):
        raise ReversePickupError("not_india_order", "reverse pickup is only for India orders")

    existing = by_awb(db, awb, for_update=True)
    if existing:
        db.rollback()
        if existing["order_id"] != oid:
            raise ReversePickupError("awb_in_use", "this AWB is recorded on another order", 409)
        if existing["status"] != ST_BOOKED:
            raise ReversePickupError("awb_cancelled", "this AWB was cancelled; book a new waybill", 409)
        return existing, False
    active = active_for_order(db, oid, for_update=True)
    if active:
        db.rollback()
        raise ReversePickupError("active_pickup_exists",
                                 "order already has an active reverse pickup (AWB %s)" % active["awb"], 409)
    case = case_for_order(db, oid, for_update=True)
    if request_blocks_booking(case):
        db.rollback()
        raise ReversePickupError(
            "request_not_approved",
            "the customer's return request is %s; a pickup is booked only once it is %s"
            % (case["request_status"], REQ_APPROVED), 409)
    if not case or case["fee_state"] not in FEE_SETTLED:
        db.rollback()
        raise ReversePickupError(
            "fee_not_settled",
            "reverse-pickup fee is %s; a pickup is booked only when it is PAID or WAIVED"
            % (case["fee_state"] if case else "not recorded"), 409)

    forward = _clip(body.get("forward_awb"), 64).upper()
    on_record = reship.shipments_for_orders(db, [oid]).get(oid)
    if on_record and on_record[0] and forward and forward != on_record[0].upper():
        db.rollback()
        raise ReversePickupError("forward_awb_mismatch",
                                 "forward_awb does not match the order's shipment on record", 409)
    forward = forward or (on_record[0] if on_record else "")
    pickup = body.get("pickup") if isinstance(body.get("pickup"), dict) else {}
    row_uuid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO order_reverse_pickups (pickup_uuid, order_id, customer_id, site_from, "
        "courier, awb, reference, reason, remarks, forward_awb, pickup_city, pickup_pin, "
        "status, booked_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (row_uuid, oid, head.get("customer_id"), head.get("site_from"), COURIER, awb,
         _clip(body.get("reference"), 64), _clip(body.get("reason"), 255),
         _clip(body.get("remarks"), 500), forward, _clip(pickup.get("city"), 64),
         _clip(pickup.get("pin"), 12), ST_BOOKED, _clip(operator, 191)))
    add_history(cur, oid, "Reverse pickup booked with Delhivery, AWB %s%s (forward AWB %s unchanged) by %s"
                % (awb, " ref %s" % _clip(body.get("reference"), 64) if body.get("reference") else "",
                   forward or "n/a", _clip(operator, 120)), head.get("site_from"))
    reship.emit(db, EV_BOOKED, oid, row_uuid, head.get("customer_id"),
                {"awb": awb, "operator": _clip(operator, 120)}, key=row_uuid, commit=False)
    row = by_awb(db, awb)
    queue_ops_event(db, EV_BOOKED, oid,
                    {"courier": COURIER, "awb": awb, "reference": row.get("reference") or None,
                     "forward_awb": forward or None, "booked_by": _clip(operator, 191),
                     "booked_at": _iso(row.get("booked_at"))},
                    case=case, pickup=row, key=row_uuid, commit=False)
    db.commit()
    return row, True


def cancel(db, order_id, body, operator):
    """Cancel an accepted booking. Returns ``(row, changed)``; cancelling an
    already cancelled AWB returns it unchanged."""
    ensure_schema(db)
    oid = resolve_order_id(db, order_id)
    if not oid:
        raise ReversePickupError("not_found", "order not found", 404)
    awb = _normal_awb(body.get("awb"))
    # Case row first, then pickup row: the same order mark_received locks
    # them in, so a receipt and a cancel of one parcel serialize.
    case = case_for_order(db, oid, for_update=True)
    row = by_awb(db, awb, for_update=True) if awb else active_for_order(db, oid, for_update=True)
    if not row or row["order_id"] != oid:
        db.rollback()
        raise ReversePickupError("not_found", "no reverse pickup with this AWB on this order", 404)
    if row["status"] == ST_CANCELLED:
        db.rollback()
        return row, False
    if case and case.get("received_at") and case.get("received_awb") == row["awb"]:
        db.rollback()
        raise ReversePickupError("pickup_already_received",
                                 "the parcel for AWB %s has been received; it cannot be cancelled"
                                 % row["awb"], 409)
    cur = db.cursor()
    cur.execute("UPDATE order_reverse_pickups SET status=%s, cancelled_by=%s, cancelled_at=NOW() "
                "WHERE id=%s AND status=%s", (ST_CANCELLED, _clip(operator, 191), row["id"], ST_BOOKED))
    add_history(cur, oid, "Reverse pickup cancelled with Delhivery, AWB %s, by %s"
                % (row["awb"], _clip(operator, 120)), row.get("site_from"))
    reship.emit(db, EV_CANCELLED, oid, row["pickup_uuid"], row.get("customer_id"),
                {"awb": row["awb"], "operator": _clip(operator, 120)}, key=row["pickup_uuid"], commit=False)
    row = by_awb(db, row["awb"])
    queue_ops_event(db, EV_CANCELLED, oid,
                    {"awb": row["awb"], "cancelled_by": _clip(operator, 191),
                     "cancelled_at": _iso(row.get("cancelled_at"))},
                    case=case_for_order(db, oid), pickup=row, key=row["pickup_uuid"], commit=False)
    db.commit()
    return row, True


def _host_url(host, path):
    host = (host or "optiwar.in").strip()
    if not host.startswith("http"):
        host = "https://" + host
    return host.rstrip("/") + path


def _wa_components(event_type, fields):
    comps = {"body_1": {"type": "text", "value": fields["order_id"]},
             "body_2": {"type": "text", "value": fields["awb"]}}
    if event_type == EV_BOOKED:
        comps["body_3"] = {"type": "text", "value": fields["track_url"]}
    return comps


def notify(db, event_type, row, host, mailer=None, whatsapp=None, environ=None):
    """Tell the customer once per channel for one booking/cancellation.
    The claim is a ``reverse_pickup.notified`` event keyed on (pickup,
    event, channel), so a replay sends nothing."""
    env = os.environ if environ is None else environ
    if not customer_enabled(env):
        return notification_state(db, row, event_type, environ=env)
    cur = db.cursor()
    acct = reship._account(cur, row.get("customer_id"))
    fields = {"name": (acct.get("customer_name") or "").strip() or "Customer",
              "order_id": row["order_id"], "awb": row["awb"],
              "track_url": reship.tracking_url(COURIER, row["awb"]) or "",
              "support": reship.SUPPORT_EMAIL,
              "change_email": CHANGE_EMAIL,
              "terms_url": _host_url(host, TERMS_URL),
              "url": reship.my_orders_url(host)}
    key = row["pickup_uuid"]

    def claim(channel):
        return reship.emit(db, EV_NOTIFIED, row["order_id"], key, row.get("customer_id"),
                           {"event": event_type, "channel": channel}, key=key,
                           suffix="%s:%s" % (event_type, channel))

    def tell_ops(ev, channel, extra=None):
        data = {"for_event": event_type, "notified_event": event_type, "channel": channel}
        data.update(extra or {})
        queue_ops_event(db, ev, row["order_id"], data, case=case_for_order(db, row["order_id"]),
                        pickup=row, key=key, suffix="%s:%s" % (event_type, channel))

    def failed(channel, exc):
        reship.emit(db, EV_NOTIFY_FAILED, row["order_id"], key, row.get("customer_id"),
                    {"event": event_type, "channel": channel, "error": str(exc)[:160]},
                    key=key, suffix="%s:%s:fail" % (event_type, channel))
        tell_ops(EV_NOTIFY_FAILED, channel, {"reason": str(exc)[:160], "error": str(exc)[:160]})

    email = (acct.get("customer_email") or "").strip()
    if email and "@" in email and claim("email"):
        subject, text = EMAILS[event_type]
        try:
            (mailer or reship._default_mailer)(email, subject.format(**fields), text.format(**fields))
        except Exception as exc:  # noqa: BLE001
            failed("email", exc)
        else:
            tell_ops(EV_NOTIFIED, "email")

    phone = (acct.get("customer_phone") or "").strip()
    approved = str(env.get(WA_APPROVED_ENV, "")).strip().lower() in ("1", "true", "yes")
    if phone and approved and claim("whatsapp"):
        tpl = env.get(reship.WA_TEMPLATE_PREFIX_ENV, "") + _WA_TEMPLATES[event_type]
        try:
            r = (whatsapp or reship._default_whatsapp)(
                phone.replace("+", "").replace(" ", "").replace("-", ""), tpl,
                _wa_components(event_type, fields)) or {}
            if not r.get("ok"):
                failed("whatsapp", r.get("error") or "not accepted")
            else:
                tell_ops(EV_NOTIFIED, "whatsapp")
        except Exception as exc:  # noqa: BLE001
            failed("whatsapp", exc)
    return notification_state(db, row, event_type, environ=env)


def notification_state(db, row, event_type, environ=None):
    """``{"sent": [{channel, at}], "failed": n, "skipped": [...]}`` for one
    event of one pickup, read back from the claims."""
    env = os.environ if environ is None else environ
    cur = db.cursor()
    cur.execute("SELECT event_type, payload, created_at FROM reship_events "
                "WHERE reship_uuid=%s AND event_type IN (%s,%s) ORDER BY id",
                (row["pickup_uuid"], EV_NOTIFIED, EV_NOTIFY_FAILED))
    claimed, failed = {}, set()
    for ev in cur.fetchall():
        p = json.loads(ev["payload"] or "{}")
        if p.get("event") != event_type:
            continue
        if ev["event_type"] == EV_NOTIFIED:
            claimed[p.get("channel")] = ev["created_at"]
        else:
            failed.add(p.get("channel"))
    sent = [{"channel": ch, "at": at.isoformat() if hasattr(at, "isoformat") else str(at)}
            for ch, at in claimed.items() if ch not in failed]
    skipped = []
    if not claimed and not customer_enabled(env):
        return {"sent": sent, "failed": len(failed),
                "skipped": [{"channel": ch, "reason": "customer_notifications_off"}
                            for ch in ("email", "whatsapp")]}
    if "whatsapp" not in claimed:
        approved = str(env.get(WA_APPROVED_ENV, "")).strip().lower() in ("1", "true", "yes")
        skipped.append({"channel": "whatsapp",
                        "reason": "template_not_approved" if not approved else "no_phone"})
    if "email" not in claimed:
        skipped.append({"channel": "email", "reason": "no_email"})
    return {"sent": sent, "failed": len(failed), "skipped": skipped}


def ops_view(db, row, event_type):
    return {"id": row["pickup_uuid"], "order_id": row["order_id"], "status": row["status"],
            "courier": row["courier"], "awb": row["awb"], "reference": row.get("reference"),
            "forward_awb": row.get("forward_awb"),
            "booked_by": row.get("booked_by"), "booked_at": str(row.get("booked_at") or ""),
            "cancelled_at": str(row["cancelled_at"]) if row.get("cancelled_at") else None,
            "notification_state": notification_state(db, row, event_type)}


def public_view(row):
    """What My Orders shows: state, AWB and its tracking link. Nothing Ops
    typed (reason, remarks, operator) reaches the customer."""
    if not row:
        return None
    return {"state": row["status"], "awb": row["awb"], "courier": row["courier"],
            "track_url": reship.tracking_url(COURIER, row["awb"]),
            "booked_at": row.get("booked_at"), "cancelled_at": row.get("cancelled_at")}


# --------------------------------------------------------------------------
# the return case and its fee: Optiwar is the authority
# --------------------------------------------------------------------------

def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _is_duplicate_key(exc):
    return bool(exc.args and exc.args[0] in (1062, 1586)) or "Duplicate entry" in str(exc)


def case_for_order(db, order_id, for_update=False):
    """The order's latest return case, or None."""
    cur = db.cursor()
    cur.execute("SELECT * FROM reverse_pickup_cases WHERE order_id=%s ORDER BY case_no DESC LIMIT 1"
                + (" FOR UPDATE" if for_update else ""), (order_id,))
    return cur.fetchone()


def case_by_uuid(db, case_uuid):
    cur = db.cursor()
    cur.execute("SELECT * FROM reverse_pickup_cases WHERE case_uuid=%s", (case_uuid,))
    return cur.fetchone()


def latest_for_order(db, order_id, for_update=False):
    cur = db.cursor()
    cur.execute("SELECT * FROM order_reverse_pickups WHERE order_id=%s ORDER BY id DESC LIMIT 1"
                + (" FOR UPDATE" if for_update else ""), (order_id,))
    return cur.fetchone()


def fee_summary(case):
    """The financial state every Ops event carries."""
    if not case:
        return {"state": None, "amount_minor": FEE_MINOR, "refunded_minor": 0,
                "currency": FEE_CURRENCY}
    return {"state": case["fee_state"], "amount_minor": int(case["fee_amount_minor"]),
            "refunded_minor": int(case.get("fee_refunded_minor") or 0),
            "currency": case["fee_currency"]}


def fee_view(case):
    out = fee_summary(case)
    out["paid_at"] = _iso(case.get("fee_paid_at")) if case else None
    out["razorpay_payment_id"] = case.get("razorpay_payment_id") if case else None
    out["waiver"] = ({"reason_code": case["waiver_reason_code"],
                      "note": case.get("waiver_note") or None,
                      "operator": case.get("waived_by"), "at": _iso(case.get("waived_at")),
                      "waived_by": case.get("waived_by"),
                      "waived_at": _iso(case.get("waived_at"))}
                     if case and case.get("waived_at") else None)
    out["refunds"] = []
    return out


def queue_ops_event(db, event, order_id, data, case=None, pickup=None, key=None, suffix="",
                    commit=True):
    """Write one Ops event to the outbox. The body is stored as the exact bytes
    later signed and sent; the same (event, key, suffix) is written once."""
    eid = reship.event_id_for(event, key or (case or {}).get("case_uuid")
                              or (pickup or {}).get("pickup_uuid") or order_id, "ops:" + suffix)
    at = _iso(reship.db_now(db))
    case_uuid = case.get("case_uuid") if case else None
    pickup_uuid = pickup.get("pickup_uuid") if pickup else None
    payload = {"event_id": eid, "event": event, "at": at, "occurred_at": at,
               "order_ref": order_id,
               "request_id": case_uuid, "case_id": case_uuid,
               "pickup_uuid": pickup_uuid, "reverse_pickup_id": pickup_uuid,
               "awb": pickup.get("awb") if pickup else None,
               "fee": fee_summary(case), "data": data or {}}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    cur = db.cursor()
    cur.execute("INSERT IGNORE INTO reverse_pickup_ops_outbox (event_id, event, order_id, case_uuid, "
                "body, status) VALUES (%s,%s,%s,%s,%s,%s)",
                (eid, event, order_id, payload["case_id"], body, OUT_PENDING))
    if commit:
        db.commit()
    return eid


def deliver_ops_events(db, environ=None, http_post=None, limit=50, logger=None):
    """POST undelivered outbox events to ``RESHIP_OPS_WEBHOOK_URL`` in the
    order they were written, signed with ``RESHIP_OPS_WEBHOOK_SECRET`` over
    the exact body. A 2xx marks one SENT; anything else marks it FAILED and
    stops the batch, so Ops never receives a later event before an earlier
    one. The next call (or the reconcile cron) retries."""
    env = os.environ if environ is None else environ
    summary = {"sent": 0, "failed": 0}
    url = str(env.get(reship.OPS_WEBHOOK_URL_ENV, "")).strip()
    if not url:
        summary["skipped"] = "no_webhook_url"
        return summary
    secret = str(env.get(reship.OPS_WEBHOOK_SECRET_ENV, "")).strip().encode("utf-8")
    cur = db.cursor()
    cur.execute("SELECT id, event_id, event, body FROM reverse_pickup_ops_outbox "
                "WHERE status IN (%s,%s) ORDER BY id LIMIT %s", (OUT_PENDING, OUT_FAILED, int(limit)))
    rows = cur.fetchall()
    db.commit()
    for r in rows:
        body = r["body"].encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Optiwar-Event": r["event"],
                   "X-Optiwar-Event-Id": r["event_id"]}
        status = None
        if not secret:
            ok, err = False, "%s not configured; event not sent" % reship.OPS_WEBHOOK_SECRET_ENV
        else:
            headers["X-Optiwar-Signature"] = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
            try:
                status = int((http_post or reship._default_http_post)(url, body, headers) or 0)
                ok = 200 <= status < 300
                err = None if ok else "http %s" % status
            except Exception as exc:  # noqa: BLE001
                ok, err = False, str(exc)[:160]
        cur.execute("UPDATE reverse_pickup_ops_outbox SET status=%s, attempts=attempts+1, "
                    "last_status=%s, last_error=%s, sent_at=IF(%s, NOW(), sent_at) WHERE id=%s",
                    (OUT_SENT if ok else OUT_FAILED, status, err, ok, r["id"]))
        db.commit()
        if not ok:
            summary["failed"] += 1
            if logger:
                logger.warning("REVERSE_PICKUP_OPS_SYNC_FAILED event:%s %s %s"
                               % (r["event_id"], r["event"], err))
            break
        summary["sent"] += 1
    return summary


def waive(db, order_id, body, operator):
    """Ops waives the fee. Works when no case exists yet (a pickup booked
    before the fee flow); never asks the customer for anything. Returns
    ``(case, changed)``: the same waiver again is ``changed`` False, a
    different one is refused."""
    ensure_schema(db)
    oid = resolve_order_id(db, order_id)
    if not oid:
        raise ReversePickupError("not_found", "order not found", 404)
    code = str(body.get("reason_code") or "").strip().upper()
    if code not in WAIVER_REASONS:
        raise ReversePickupError("invalid_reason_code",
                                 "reason_code must be one of %s" % ", ".join(WAIVER_REASONS))
    note = _clip(body.get("note"), 500)
    if code == "OTHER" and not note:
        raise ReversePickupError("note_required", "reason_code OTHER requires a note")
    cur = db.cursor()
    head = reship._order_head(cur, oid)
    if not reship.is_india_host(head.get("site_from")):
        raise ReversePickupError("not_india_order", "reverse pickup is only for India orders")
    who = _clip(operator, 191)
    for attempt in (1, 2):
        case = case_for_order(db, oid, for_update=True)
        if case:
            if case["fee_state"] == FEE_WAIVED:
                db.rollback()
                if case["waiver_reason_code"] == code and (case.get("waiver_note") or "") == note:
                    return case, False
                raise ReversePickupError("waiver_exists", "the fee is already waived (%s)"
                                         % case["waiver_reason_code"], 409)
            if case["fee_state"] == FEE_PAID:
                db.rollback()
                raise ReversePickupError("fee_already_paid", "the fee is PAID; it cannot be waived", 409)
            if case["fee_state"] != FEE_DUE:
                db.rollback()
                raise ReversePickupError("fee_not_waivable", "the fee is %s; only a DUE fee can be waived"
                                         % case["fee_state"], 409)
            cur.execute("UPDATE reverse_pickup_cases SET fee_state=%s, waiver_reason_code=%s, "
                        "waiver_note=%s, waived_by=%s, waived_at=NOW() WHERE id=%s AND fee_state=%s",
                        (FEE_WAIVED, code, note or None, who, case["id"], FEE_DUE))
            case_uuid = case["case_uuid"]
            break
        case_uuid = str(uuid.uuid4())
        try:
            cur.execute(
                "INSERT INTO reverse_pickup_cases (case_uuid, order_id, case_no, customer_id, site_from, "
                "source, fee_state, fee_amount_minor, fee_currency, waiver_reason_code, waiver_note, "
                "waived_by, waived_at, created_by) VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)",
                (case_uuid, oid, head.get("customer_id"), head.get("site_from"), "ops_waiver",
                 FEE_WAIVED, FEE_MINOR, FEE_CURRENCY, code, note or None, who, who))
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            if attempt == 1 and _is_duplicate_key(exc):
                continue
            raise
        break
    case = case_by_uuid(db, case_uuid)
    add_history(cur, oid, "Reverse-pickup fee INR %d waived by %s, reason %s%s"
                % (FEE_MINOR // 100, _clip(operator, 120), code, " (%s)" % note if note else ""),
                head.get("site_from"))
    reship.emit(db, EV_FEE_WAIVED, oid, case_uuid, head.get("customer_id"),
                {"reason_code": code, "note": note or None, "operator": who},
                key=case_uuid, commit=False)
    queue_ops_event(db, EV_FEE_WAIVED, oid,
                    {"reason_code": code, "note": note or None, "operator": who, "waived_by": who,
                     "waived_at": _iso(case.get("waived_at"))},
                    case=case, pickup=latest_for_order(db, oid), key=case_uuid, commit=False)
    db.commit()
    return case, True


def correct_reason(db, order_id, body, operator):
    """Correct the return reason on Optiwar's record. The courier booking is
    not touched. Returns ``(row, changed, old_reason)``."""
    ensure_schema(db)
    oid = resolve_order_id(db, order_id)
    if not oid:
        raise ReversePickupError("not_found", "order not found", 404)
    reason = _clip(body.get("reason"), 255)
    if not reason:
        raise ReversePickupError("reason_required", "reason required")
    awb = _normal_awb(body.get("awb"))
    row = by_awb(db, awb, for_update=True) if awb else latest_for_order(db, oid, for_update=True)
    if not row or row["order_id"] != oid:
        db.rollback()
        raise ReversePickupError("not_found", "no reverse pickup with this AWB on this order", 404)
    old = row.get("reason") or ""
    if old == reason:
        db.rollback()
        return row, False, old
    cur = db.cursor()
    cur.execute("UPDATE order_reverse_pickups SET reason=%s WHERE id=%s", (reason, row["id"]))
    case = case_for_order(db, oid, for_update=True)
    if case:
        cur.execute("UPDATE reverse_pickup_cases SET return_reason=%s WHERE id=%s", (reason, case["id"]))
    at = reship.db_now(db)
    who = _clip(operator, 191)
    add_history(cur, oid, 'Return reason for reverse pickup AWB %s corrected from "%s" to "%s" by %s'
                % (row["awb"], old or "-", reason, _clip(operator, 120)), row.get("site_from"))
    reship.emit(db, EV_REASON_CORRECTED, oid, row["pickup_uuid"], row.get("customer_id"),
                {"awb": row["awb"], "operator": who, "old_reason": old or None,
                 "new_reason": reason, "at": _iso(at)},
                key=row["pickup_uuid"], suffix=str(uuid.uuid4()), commit=False)
    db.commit()
    return by_awb(db, row["awb"]), True, old


def request_blocks_booking(case):
    """A customer's request is booked only once Ops approved it."""
    return bool(case and case.get("request_status") and case["request_status"] != REQ_APPROVED)


def _queue_state(case, pickup):
    if case and case.get("completed_outcome") == OUTCOME_NOT_APPROVED:
        return "NOT_APPROVED"
    if case and case.get("completed_at"):
        return "COMPLETED"
    if case and case.get("request_status") == REQ_SUBMITTED:
        return "REQUESTED"
    if case and case.get("request_status") == REQ_INFO:
        return "INFO_REQUESTED"
    if case and case.get("consent_at"):
        return "READY_TO_DISPATCH"
    if case and case.get("inspected_at"):
        return "DEFECT_CONFIRMED" if case.get("inspection_defect") else "AWAITING_CUSTOMER_CONSENT"
    if case and case.get("received_at"):
        return "AWAITING_INSPECTION"
    if pickup and pickup["status"] == ST_BOOKED:
        return "PICKUP_BOOKED"
    if not case:
        return "FEE_NOT_RECORDED"
    if case["fee_state"] == FEE_DUE:
        return "AWAITING_FEE"
    if case["fee_state"] in FEE_SETTLED:
        return "READY_TO_BOOK"
    return case["fee_state"]


def request_view(case, reason=None):
    """The return request as Ops sees it; the route adds signed photo links."""
    if not case:
        return None
    return {"id": case["case_uuid"], "source": case["source"],
            "declared_reason": reason or case.get("return_reason"),
            "reason": reason or case.get("return_reason"),
            "reason_code": case.get("reason_code"),
            "description": case.get("defect_description") or None,
            "status": case.get("request_status"),
            "requested_at": _iso(case.get("created_at")),
            "declarations": ({"version": case["declarations_version"],
                              "sha256": case.get("declarations_sha256"),
                              "accepted_at": _iso(case.get("declarations_accepted_at")),
                              "ip": case.get("declarations_ip")}
                             if case.get("declarations_version") else None),
            "decision": ({"status": case["request_status"], "note": case.get("decision_note") or None,
                          "decided_by": case.get("decided_by"),
                          "decided_at": _iso(case.get("decided_at"))}
                         if case.get("decided_at") else None),
            "photos": []}


def state_view(db, order_id):
    """Everything Ops needs about one order's return, financial state
    included, so nothing has to be reconstructed. None for an unknown order."""
    ensure_schema(db)
    oid = resolve_order_id(db, order_id)
    if not oid:
        return None
    case = case_for_order(db, oid)
    cur = db.cursor()
    cur.execute("SELECT * FROM order_reverse_pickups WHERE order_id=%s ORDER BY id", (oid,))
    pickups = cur.fetchall()
    db.commit()
    latest = pickups[-1] if pickups else None
    original = reship.shipments_for_orders(db, [oid]).get(oid)
    queue_state = _queue_state(case, latest)
    reason = (case or {}).get("return_reason") or (latest or {}).get("reason") or None
    rp = None
    if latest:
        rp = ops_view(db, latest, EV_BOOKED if latest["status"] == ST_BOOKED else EV_CANCELLED)
        rp.update({"reason": latest.get("reason") or None,
                   "received_at": _iso((case or {}).get("received_at")),
                   "inspection": ({"manufacturing_defect": bool(case.get("inspection_defect")),
                                   "at": _iso(case.get("inspected_at")),
                                   "operator": case.get("inspected_by")}
                                  if case and case.get("inspected_at") else None),
                   "consent_at": _iso((case or {}).get("consent_at")),
                   "forward": None})
    return {
        "order_id": oid,
        "order_ref": oid,
        "case_id": case["case_uuid"] if case else None,
        "queue_state": queue_state,
        "label": QUEUE_LABELS.get(queue_state, queue_state.replace("_", " ")),
        "booking_allowed": bool(case and case["fee_state"] in FEE_SETTLED
                                and not request_blocks_booking(case)
                                and not (latest and latest["status"] == ST_BOOKED)),
        "request": request_view(case, reason),
        "fee": fee_view(case) if case else None,
        "reverse_pickup": rp,
        "reverse_pickups": [{"id": p["pickup_uuid"], "awb": p["awb"], "status": p["status"],
                             "reason": p.get("reason") or None, "booked_at": _iso(p.get("booked_at")),
                             "cancelled_at": _iso(p.get("cancelled_at"))} for p in pickups],
        "received": received_view(case),
        "inspection": inspection_view(case),
        "consent": consent_view(case),
        "notifications": notice_view(db, case["case_uuid"]) if case else [],
        "forward_shipment": {"original": ({"awb": original[0], "courier": original[1]}
                                          if original and original[0] else None),
                             "replacement": None},
    }


def ops_queue(db, limit=200):
    """Open return cases, and booked pickups that pre-date the fee flow, for
    the Ops queue. Order reference and states only."""
    ensure_schema(db)
    cur = db.cursor()
    cur.execute("SELECT * FROM reverse_pickup_cases WHERE completed_at IS NULL "
                "ORDER BY id DESC LIMIT %s", (int(limit),))
    cases = cur.fetchall()
    cur.execute("SELECT p.* FROM order_reverse_pickups p JOIN (SELECT MAX(id) AS id "
                "FROM order_reverse_pickups GROUP BY order_id) l ON l.id=p.id")
    latest = {p["order_id"]: p for p in cur.fetchall()}
    db.commit()
    items, seen = [], set()

    def item(case, pickup, oid):
        state = _queue_state(case, pickup)
        return {"order_id": oid, "order_ref": oid,
                "request_id": case["case_uuid"] if case else None,
                "case_id": case["case_uuid"] if case else None,
                "requested_at": _iso(case.get("created_at")) if case else None,
                "declared_reason": ((case or {}).get("return_reason")
                                    or (pickup or {}).get("reason") or None),
                "queue_state": state, "label": QUEUE_LABELS.get(state, state.replace("_", " ")),
                "fee": fee_summary(case),
                "awb": pickup["awb"] if pickup else None,
                "pickup_awb": pickup["awb"] if pickup else None,
                "pickup_status": pickup["status"] if pickup else None,
                "updated_at": _iso(max(x for x in ((case or {}).get("updated_at"),
                                                   (pickup or {}).get("updated_at")) if x))}

    for c in cases:
        seen.add(c["order_id"])
        items.append(item(c, latest.get(c["order_id"]), c["order_id"]))
    for oid, p in latest.items():
        if oid not in seen and p["status"] == ST_BOOKED:
            items.append(item(None, p, oid))
    items.sort(key=lambda i: i["updated_at"] or "", reverse=True)
    return items


# --------------------------------------------------------------------------
# the parcel back at Optiwar: receipt, inspection, consent
# --------------------------------------------------------------------------

def _parse_at(value, field):
    """An ISO-8601 time from Ops as a naive server-local datetime, or None
    (meaning now)."""
    if value in (None, ""):
        return None
    try:
        at = datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ReversePickupError("invalid_%s" % field, "%s must be an ISO-8601 time" % field)
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at


def received_view(case):
    if not case or not case.get("received_at"):
        return None
    return {"awb": case.get("received_awb"), "condition": case.get("received_condition"),
            "notes": case.get("received_notes") or None, "received_by": case.get("received_by"),
            "received_at": _iso(case.get("received_at"))}


def inspection_view(case):
    if not case or not case.get("inspected_at"):
        return None
    return {"manufacturing_defect": bool(case.get("inspection_defect")),
            "remarks": case.get("inspection_remarks") or None,
            "inspected_by": case.get("inspected_by"), "inspected_at": _iso(case.get("inspected_at"))}


def consent_view(case):
    if not case or not case.get("consent_at"):
        return None
    return {"record": CONSENT_RECORD, "recorded_by": case.get("consent_recorded_by"),
            "consent_at": _iso(case.get("consent_at")), "message_id": case.get("consent_message_id")}


def _case_for_action(db, order_id):
    ensure_schema(db)
    oid = resolve_order_id(db, order_id)
    if not oid:
        raise ReversePickupError("not_found", "order not found", 404)
    case = case_for_order(db, oid, for_update=True)
    if not case:
        db.rollback()
        raise ReversePickupError("no_case", "this order has no reverse-pickup case", 409)
    return oid, case


def _audit(db, event_type, case, payload, suffix=""):
    reship.emit(db, event_type, case["order_id"], case["case_uuid"], case.get("customer_id"),
                payload, key=case["case_uuid"], suffix=suffix, commit=False)


def mark_received(db, order_id, body, operator):
    """Ops has the parcel. Records the receipt on the return case only; the
    RTO / reship workflow is a different mechanism and is not touched.
    Returns ``(case, changed)``."""
    raw = str(body.get("condition") or "").strip().lower()
    condition = next((c for c in RECEIVED_CONDITIONS if c.lower() == raw), None)
    if not condition:
        raise ReversePickupError("invalid_condition",
                                 "condition must be one of %s" % ", ".join(RECEIVED_CONDITIONS))
    awb = _normal_awb(body.get("awb"))
    if not awb:
        raise ReversePickupError("awb_required", "awb required")
    at = _parse_at(body.get("received_at"), "received_at")
    notes = _clip(body.get("notes"), 500)
    oid, case = _case_for_action(db, order_id)
    pickup = by_awb(db, awb, for_update=True)
    if not pickup or pickup["order_id"] != oid:
        db.rollback()
        raise ReversePickupError("awb_mismatch", "AWB %s is not a reverse pickup of this order" % awb, 409)
    if case.get("received_at"):
        db.rollback()
        if (case.get("received_awb"), case.get("received_condition"),
                case.get("received_notes") or "") == (awb, condition, notes):
            return case, False
        raise ReversePickupError("received_exists", "the parcel was already recorded as received at %s"
                                 % _iso(case["received_at"]), 409)
    if pickup["status"] != ST_BOOKED:
        db.rollback()
        raise ReversePickupError("pickup_cancelled", "reverse pickup AWB %s is cancelled" % awb, 409)
    who = _clip(operator, 191)
    cur = db.cursor()
    cur.execute("UPDATE reverse_pickup_cases SET received_by=%s, received_at=COALESCE(%s, NOW()), "
                "received_awb=%s, received_condition=%s, received_notes=%s WHERE id=%s",
                (who, at, awb, condition, notes or None, case["id"]))
    case = case_by_uuid(db, case["case_uuid"])
    add_history(cur, oid, "Reverse-pickup parcel AWB %s received by %s, condition: %s%s"
                % (awb, _clip(operator, 120), condition, " (%s)" % notes if notes else ""),
                case.get("site_from"))
    data = {"awb": awb, "condition": condition, "notes": notes or None, "received_by": who,
            "received_at": _iso(case["received_at"])}
    _audit(db, EV_RECEIVED, case, data)
    queue_ops_event(db, EV_RECEIVED, oid, data, case=case, pickup=pickup, key=case["case_uuid"],
                    commit=False)
    enqueue_notice(db, case, EV_RECEIVED)
    db.commit()
    return case, True


def record_inspection(db, order_id, body, operator):
    """A person at Ops inspected the parcel. Only this endpoint sets the
    result; nothing infers it. Returns ``(case, changed)``."""
    defect = body.get("manufacturing_defect")
    if not isinstance(defect, bool):
        raise ReversePickupError("invalid_manufacturing_defect", "manufacturing_defect must be true or false")
    remarks = _clip(body.get("remarks"), 1000)
    at = _parse_at(body.get("inspected_at"), "inspected_at")
    oid, case = _case_for_action(db, order_id)
    if not case.get("received_at"):
        db.rollback()
        raise ReversePickupError("not_received", "the parcel has not been recorded as received", 409)
    if case.get("inspected_at"):
        db.rollback()
        if (bool(case["inspection_defect"]), case.get("inspection_remarks") or "") == (defect, remarks):
            return case, False
        raise ReversePickupError("inspection_exists", "an inspection was already recorded at %s"
                                 % _iso(case["inspected_at"]), 409)
    who = _clip(operator, 191)
    cur = db.cursor()
    cur.execute("UPDATE reverse_pickup_cases SET inspection_defect=%s, inspection_remarks=%s, "
                "inspected_by=%s, inspected_at=COALESCE(%s, NOW()) WHERE id=%s",
                (1 if defect else 0, remarks or None, who, at, case["id"]))
    case = case_by_uuid(db, case["case_uuid"])
    add_history(cur, oid, "Reverse-pickup inspection by %s: manufacturing defect %s%s"
                % (_clip(operator, 120), "CONFIRMED" if defect else "NOT CONFIRMED",
                   " (%s)" % remarks if remarks else ""), case.get("site_from"))
    data = {"manufacturing_defect": defect, "remarks": remarks or None, "inspected_by": who,
            "inspected_at": _iso(case["inspected_at"]),
            "refund_eligible": bool(defect and case["fee_state"] == FEE_PAID)}
    _audit(db, EV_INSPECTED, case, data)
    queue_ops_event(db, EV_INSPECTED, oid, data, case=case, pickup=latest_for_order(db, oid),
                    key=case["case_uuid"], commit=False)
    notice = inspection_notice(case)
    if notice:
        enqueue_notice(db, case, notice)
    db.commit()
    return case, True


def inspection_notice(case):
    """Which inspection notice the customer gets now, or None (a confirmed
    defect on a PAID fee is told with the refund)."""
    if not case or not case.get("inspected_at"):
        return None
    if not case.get("inspection_defect"):
        return NOTICE_INSPECTION_NO_DEFECT
    return NOTICE_INSPECTION_DEFECT_WAIVED if case["fee_state"] == FEE_WAIVED else None


def record_consent(db, order_id, body, operator):
    """The customer replied by email asking for the product back after an
    inspection that did not confirm the defect. Returns ``(case, changed)``."""
    message_id = _clip(body.get("message_id"), 255)
    if not message_id:
        raise ReversePickupError("message_id_required", "message_id (the email Message-ID) required")
    at = _parse_at(body.get("consent_at"), "consent_at")
    oid, case = _case_for_action(db, order_id)
    if not case.get("inspected_at"):
        db.rollback()
        raise ReversePickupError("not_inspected", "no inspection has been recorded", 409)
    if case.get("inspection_defect"):
        db.rollback()
        raise ReversePickupError("consent_not_applicable",
                                 "the defect was confirmed; no customer consent is needed", 409)
    if case.get("consent_at"):
        db.rollback()
        if case.get("consent_message_id") == message_id:
            return case, False
        raise ReversePickupError("consent_exists", "consent was already recorded at %s"
                                 % _iso(case["consent_at"]), 409)
    who = _clip(operator, 191)
    cur = db.cursor()
    cur.execute("UPDATE reverse_pickup_cases SET consent_recorded_by=%s, consent_at=COALESCE(%s, NOW()), "
                "consent_message_id=%s WHERE id=%s", (who, at, message_id, case["id"]))
    case = case_by_uuid(db, case["case_uuid"])
    add_history(cur, oid, "%s: customer asked by email for the product back (Message-ID %s), recorded by %s"
                % (CONSENT_RECORD, message_id, _clip(operator, 120)), case.get("site_from"))
    data = {"record": CONSENT_RECORD, "recorded_by": who, "consent_at": _iso(case["consent_at"]),
            "channel": "email", "message_id": message_id}
    _audit(db, CONSENT_RECORD, case, data)
    queue_ops_event(db, EV_CONSENT, oid, data, case=case, pickup=latest_for_order(db, oid),
                    key=case["case_uuid"], commit=False)
    db.commit()
    return case, True


def _notice_suffix(notice, channel="email"):
    return "case:%s:%s" % (notice, channel)


def _notice_row(db, case_uuid, notice, channel="email", for_update=False):
    cur = db.cursor()
    cur.execute("SELECT * FROM reverse_pickup_notifications WHERE case_uuid=%s AND "
                "notification_type=%s AND channel=%s" + (" FOR UPDATE" if for_update else ""),
                (case_uuid, notice, channel))
    return cur.fetchone()


def enqueue_notice(db, case, notice, channel="email", environ=None):
    """Record that the customer is owed ``notice`` for this case. Written in
    the transaction of the step that causes it; a second call is a no-op.
    Nothing is owed while customer notices are switched off, so turning
    them on later never sends anything retroactively."""
    if not customer_enabled(environ):
        return False
    key = case["case_uuid"]
    suffix = _notice_suffix(notice, channel)
    # A notice claimed in reship_events before this table existed keeps the
    # outcome it had then.
    claimed = reship.event_id_for(EV_NOTIFIED, key, suffix)
    failed = reship.event_id_for(EV_NOTIFY_FAILED, key, suffix + ":fail")
    cur = db.cursor()
    cur.execute("SELECT event_id FROM reship_events WHERE event_id IN (%s,%s)", (claimed, failed))
    legacy = {r["event_id"] for r in cur.fetchall()}
    status = (NOTICE_FAILED if failed in legacy else NOTICE_SENT) if claimed in legacy else NOTICE_PENDING
    cur.execute("INSERT IGNORE INTO reverse_pickup_notifications (event_id, case_uuid, order_id, "
                "notification_type, channel, status, sent_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (claimed, key, case["order_id"], notice, channel, status,
                 reship.db_now(db) if status == NOTICE_SENT else None))
    return cur.rowcount == 1


def notice_view(db, case_uuid):
    cur = db.cursor()
    cur.execute("SELECT * FROM reverse_pickup_notifications WHERE case_uuid=%s ORDER BY id", (case_uuid,))
    return [{"notification_type": n["notification_type"], "channel": n["channel"], "status": n["status"],
             "attempt_count": int(n["attempt_count"]), "last_attempt_at": _iso(n.get("last_attempt_at")),
             "last_error": n.get("last_error"), "sent_at": _iso(n.get("sent_at"))}
            for n in cur.fetchall()]


def _send_notice(db, n, case, mailer=None):
    """One attempt at one owed notice. The row is claimed (SENDING) before
    the mail is handed over, so two workers never both send it; a claim
    that was interrupted mid-send stays SENDING for a person to check
    rather than risk a second email."""
    cur = db.cursor()
    cur.execute("UPDATE reverse_pickup_notifications SET status=%s, attempt_count=attempt_count+1, "
                "last_attempt_at=NOW() WHERE id=%s AND status IN (%s,%s)",
                (NOTICE_SENDING, n["id"], NOTICE_PENDING, NOTICE_FAILED))
    claimed = cur.rowcount == 1
    db.commit()
    if not claimed:
        return "replay"
    notice, oid, key = n["notification_type"], case["order_id"], case["case_uuid"]
    suffix = _notice_suffix(notice, n["channel"])
    customer_id = case.get("customer_id") or reship._order_head(cur, oid).get("customer_id")
    acct = reship._account(cur, customer_id)
    email = (acct.get("customer_email") or "").strip()
    if not (email and "@" in email):
        cur.execute("UPDATE reverse_pickup_notifications SET status=%s, last_error=%s WHERE id=%s",
                    (NOTICE_NO_EMAIL, "no customer email on the account", n["id"]))
        db.commit()
        return "no_email"
    subject, lines = CASE_EMAILS[notice]
    fields = {"name": (acct.get("customer_name") or "").strip() or "Customer", "order_id": oid,
              "fee_line": FEE_RETAINED_LINE if case["fee_state"] != FEE_WAIVED else "",
              "reason": case.get("return_reason") or "-", "note": case.get("decision_note") or "",
              "url": MY_ORDERS_URL_IN}
    pickup = latest_for_order(db, oid)
    try:
        (mailer or reship._default_mailer)(email, subject, (CASE_EMAIL_FRAME % lines).format(**fields))
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:160]
        attempt = int(n["attempt_count"]) + 1
        cur.execute("UPDATE reverse_pickup_notifications SET status=%s, last_error=%s WHERE id=%s",
                    (NOTICE_FAILED, error, n["id"]))
        reship.emit(db, EV_NOTIFY_FAILED, oid, key, customer_id,
                    {"event": notice, "channel": n["channel"], "error": error, "attempt": attempt},
                    key=key, suffix=suffix + (":fail" if attempt == 1 else ":fail:%d" % attempt),
                    commit=False)
        if attempt == 1:
            queue_ops_event(db, EV_NOTIFY_FAILED, oid, {"for_event": notice, "notified_event": notice,
                                                       "channel": n["channel"], "reason": error,
                                                       "error": error},
                            case=case, pickup=pickup, key=key, suffix=suffix, commit=False)
        db.commit()
        return "failed"
    cur.execute("UPDATE reverse_pickup_notifications SET status=%s, sent_at=NOW(), last_error=NULL "
                "WHERE id=%s", (NOTICE_SENT, n["id"]))
    reship.emit(db, EV_NOTIFIED, oid, key, customer_id, {"event": notice, "channel": n["channel"]},
                key=key, suffix=suffix, commit=False)
    queue_ops_event(db, EV_NOTIFIED, oid, {"for_event": notice, "notified_event": notice,
                                           "channel": n["channel"]},
                    case=case, pickup=pickup, key=key, suffix=suffix, commit=False)
    db.commit()
    return "sent"


def notify_case(db, notice, case, mailer=None, environ=None):
    """Email the customer one case notice now. The notice is owed once per
    (case, notice); a send that fails is left FAILED for retry_notices.
    Returns ``"sent"``, ``"replay"``, ``"failed"``, ``"no_email"`` or ``"off"``."""
    if not customer_enabled(environ):
        return "off"
    enqueue_notice(db, case, notice, environ=environ)
    db.commit()
    n = _notice_row(db, case["case_uuid"], notice)
    if not n or n["status"] not in (NOTICE_PENDING, NOTICE_FAILED) or int(n["attempt_count"]):
        return "replay"
    return _send_notice(db, n, case, mailer=mailer)


def retry_notices(db, mailer=None, environ=None, limit=50, logger=None):
    """Send every owed notice that is not yet sent and whose last attempt is
    at least REVERSE_PICKUP_NOTICE_RETRY_MINUTES (15) old. The step that
    caused it is never repeated. Returns a summary dict."""
    env = os.environ if environ is None else environ
    out = {"due": 0, "sent": 0, "failed": 0, "no_email": 0}
    if not customer_enabled(env):
        return dict(out, off=True)
    ensure_schema(db)
    minutes = int(env.get(NOTICE_RETRY_MINUTES_ENV) or NOTICE_RETRY_MINUTES)
    cur = db.cursor()
    cur.execute("SELECT * FROM reverse_pickup_notifications WHERE status IN (%s,%s) AND "
                "(last_attempt_at IS NULL OR last_attempt_at <= NOW() - INTERVAL %s MINUTE) "
                "ORDER BY id LIMIT %s", (NOTICE_PENDING, NOTICE_FAILED, minutes, int(limit)))
    rows = cur.fetchall()
    db.commit()
    for n in rows:
        case = case_by_uuid(db, n["case_uuid"])
        if not case:
            continue
        out["due"] += 1
        result = _send_notice(db, n, case, mailer=mailer)
        if result in out:
            out[result] += 1
        if result == "failed" and logger:
            logger.warning("REVERSE_PICKUP_NOTICE_RETRY_FAILED %s case:%s attempt:%d"
                           % (n["notification_type"], n["case_uuid"], int(n["attempt_count"]) + 1))
    return out
