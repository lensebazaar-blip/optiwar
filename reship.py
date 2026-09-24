"""Return-to-origin and paid reshipment — optiwar.in only.

A parcel the courier could not deliver comes back. Two things that used to be
one status are kept apart here:

    RETURNING_TO_OPS   the courier says it is coming back (``order_status``
                       row ``Returned``, written by the courier platform).
                       Nothing here is unlocked by that alone.
    RETURNED_TO_OPS    a named Ops operator confirmed the parcel is physically
                       in hand. Only then does the customer see the reship
                       offer. This is the row in ``order_reshipments``.

From there the reship record carries its own lifecycle, separate from the
merchandise payment, which is never touched:

    RETURNED -> PAYMENT_PENDING -> PAID -> RESHIPPED      (or CANCELLED)

The fee is fixed by this module (``FEE_INR``), never by the browser; the
Razorpay order for it is a dedicated one whose notes say ``purpose=RESHIPMENT``
so no payment path can mistake it for merchandise money. Settlement applies the
same rules as ``razorpay_settlement.settle``: captured, exact amount, exact
currency, the payment belongs to this reship's own Razorpay order, and the
payment id is bound nowhere else (``payment_collector`` or another reship).
Browser callback, webhook and the reconcile worker all arrive here and the
UNIQUE key on ``razorpay_payment_id`` plus the row lock make it apply once.

Every transition writes one row to ``reship_events`` under a deterministic
event id; a notification is claimed the same way, so a replayed callback or a
second worker sends nothing twice. Delivery failure is recorded and changes
no state.

Flask-free: a DB-API connection with dict cursors, plus injected callables
for Razorpay and the notification channels.
"""
import json
import os
import uuid
from urllib.parse import quote

try:
    from .paid_orders import add_history
except ImportError:  # loaded as a plain module (tests, deploy tool)
    from paid_orders import add_history

ENABLED_ENV = "RESHIP_ENABLED_IN"
ALLOW_ORDERS_ENV = "RESHIP_ALLOW_ORDERS"          # optional: comma list, first rollout
WA_APPROVED_ENV = "RESHIP_WA_TEMPLATES_APPROVED"   # WhatsApp only once Meta approved
WA_TEMPLATE_PREFIX_ENV = "RESHIP_WA_TEMPLATE_PREFIX"

FEE_INR = 250
FEE_MINOR = FEE_INR * 100
CURRENCY = "INR"
PURPOSE = "RESHIPMENT"
RECEIPT_PREFIX = "RESHIP-"

# reship row status
ST_RETURNED = "RETURNED"            # physically back at Ops; reship available
ST_PAYMENT_PENDING = "PAYMENT_PENDING"
ST_PAID = "PAID"
ST_RESHIPPED = "RESHIPPED"
ST_CANCELLED = "CANCELLED"
ACTIVE = (ST_RETURNED, ST_PAYMENT_PENDING, ST_PAID, ST_RESHIPPED)
PAYABLE = (ST_RETURNED, ST_PAYMENT_PENDING)

# payment sub-state
PAY_NONE = "NONE"
PAY_PENDING = "PENDING"
PAY_PAID = "PAID"

# logistics state a customer or Ops is shown
LOG_RETURNING = "RETURNING_TO_OPS"
LOG_RETURNED = "RETURNED_TO_OPS"
LOG_RESHIP_PAID = "RESHIP_PAID"
LOG_RESHIPPED = "RESHIPPED"

COURIER_RETURN_STATUS = "Returned"

EV_RETURN_STARTED = "shipment.return_started"
EV_RETURNED = "shipment.returned_to_ops"
EV_AVAILABLE = "reship.available"
EV_PAYMENT_STARTED = "reship.payment_started"
EV_PAYMENT_COMPLETED = "reship.payment_completed"
EV_PAYMENT_REFUSED = "reship.payment_refused"
EV_SHIPPED = "reship.shipped"
EV_NOTIFIED = "reship.notified"
EV_NOTIFY_FAILED = "reship.notify_failed"

_EVENT_NS = uuid.UUID("2b7c1f4e-9d3a-4c58-8f0e-6a1b5d2c7e93")

# outcomes of settle_payment
APPLIED = "applied"
DUPLICATE = "duplicate"
NOT_CAPTURED = "not_captured"
UNKNOWN_RESHIP = "unknown_reship"
ORDER_MISMATCH = "order_mismatch"
AMOUNT_MISMATCH = "amount_mismatch"
CURRENCY_MISMATCH = "currency_mismatch"
ALREADY_BOUND = "already_bound"
NOT_PAYABLE = "not_payable"

RESHIPMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_reshipments (
    id                       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    reship_uuid              CHAR(36) NOT NULL,
    order_id                 VARCHAR(64) NOT NULL,
    customer_id              BIGINT NULL,
    site_from                VARCHAR(64) NULL,
    original_awb             VARCHAR(64) NOT NULL DEFAULT '',
    original_courier         VARCHAR(64) NULL,
    return_reason            VARCHAR(255) NULL,
    status                   VARCHAR(24) NOT NULL,
    payment_status           VARCHAR(16) NOT NULL DEFAULT 'NONE',
    fee_amount               INT NOT NULL,
    fee_minor                INT NOT NULL,
    fee_currency             CHAR(3) NOT NULL,
    razorpay_order_id        VARCHAR(64) NULL,
    razorpay_payment_id      VARCHAR(64) NULL,
    payment_dump             TEXT NULL,
    paid_source              VARCHAR(32) NULL,
    ops_return_confirmed_by  VARCHAR(191) NOT NULL,
    ops_return_confirmed_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at                  DATETIME NULL,
    reshipped_at             DATETIME NULL,
    new_awb                  VARCHAR(64) NULL,
    new_courier              VARCHAR(64) NULL,
    shipped_by               VARCHAR(191) NULL,
    cancelled_at             DATETIME NULL,
    cancel_reason            VARCHAR(255) NULL,
    created_at               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                             ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_reship_uuid (reship_uuid),
    UNIQUE KEY uq_reship_shipment (order_id, original_awb),
    UNIQUE KEY uq_reship_payment (razorpay_payment_id),
    UNIQUE KEY uq_reship_rzp_order (razorpay_order_id),
    KEY idx_reship_customer (customer_id, created_at),
    KEY idx_reship_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS reship_events (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    event_id    CHAR(36) NOT NULL,
    event_type  VARCHAR(48) NOT NULL,
    order_id    VARCHAR(64) NOT NULL,
    reship_uuid CHAR(36) NULL,
    customer_id BIGINT NULL,
    payload     TEXT NULL,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_re_event (event_id),
    KEY idx_re_type (event_type, created_at),
    KEY idx_re_order (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (("order_reshipments", RESHIPMENTS_SCHEMA), ("reship_events", EVENTS_SCHEMA))

_SCHEMA_READY = False


class ReshipError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def ensure_schema(db):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    cur = db.cursor()
    for _name, ddl in TABLES:
        cur.execute(ddl)
    db.commit()
    _SCHEMA_READY = True


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------

def is_india_host(host):
    h = (host or "").lower()
    return "in.optiwar.com" in h or "optiwar.in" in h


def enabled(environ=None):
    env = os.environ if environ is None else environ
    return str(env.get(ENABLED_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def order_allowed(order_id, environ=None):
    """The optional first-rollout allow-list: empty means every .in order."""
    env = os.environ if environ is None else environ
    allow = [o.strip() for o in str(env.get(ALLOW_ORDERS_ENV, "")).split(",") if o.strip()]
    return not allow or str(order_id) in allow


TRACKING_PAGES = {
    "dtdc": "https://www.dtdc.com/track",
    "delhivery": "https://www.delhivery.com/track-v2/package/{awb}",
}


def tracking_url(courier, awb):
    """The courier's public tracking page for an AWB, or None when the
    courier is unknown. DTDC has no deep link, so its page is opened and the
    customer pastes the AWB shown next to it."""
    awb = (awb or "").strip()
    page = TRACKING_PAGES.get((courier or "").strip().lower())
    if not awb or not page:
        return None
    return page.format(awb=quote(awb, safe=""))


def workflow_open(host, order_site, order_id, environ=None):
    """Whether the reship workflow exists at all for this request + order:
    the flag is on, the request is on the India site, the order was taken on
    the India site, and the order is inside the rollout allow-list."""
    return (enabled(environ) and is_india_host(host) and is_india_host(order_site)
            and order_allowed(order_id, environ))


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

def event_id_for(event_type, key, suffix=""):
    return str(uuid.uuid5(_EVENT_NS, "%s:%s:%s" % (event_type, key, suffix)))


def emit(db, event_type, order_id, reship_uuid=None, customer_id=None, payload=None,
         key=None, suffix="", commit=True):
    """Write one event; True when it was new. ``key`` defaults to the reship
    uuid, else the order id, so a retry of the same transition is a no-op."""
    eid = event_id_for(event_type, key or reship_uuid or order_id, suffix)
    cur = db.cursor()
    cur.execute(
        "INSERT IGNORE INTO reship_events (event_id, event_type, order_id, reship_uuid, "
        "customer_id, payload) VALUES (%s,%s,%s,%s,%s,%s)",
        (eid, event_type, str(order_id), reship_uuid, customer_id,
         json.dumps(payload or {}, default=str)))
    new = cur.rowcount == 1
    if commit:
        db.commit()
    return new


def events_for(db, order_id):
    cur = db.cursor()
    cur.execute("SELECT event_type, reship_uuid, payload, created_at FROM reship_events "
                "WHERE order_id=%s ORDER BY id", (str(order_id),))
    return cur.fetchall()


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def _order_head(cur, order_id):
    cur.execute("SELECT MIN(customer_id) AS customer_id, MIN(site_from) AS site_from, "
                "COUNT(*) AS line_count, MAX(is_test) AS is_test FROM orders WHERE order_id=%s",
                (str(order_id),))
    row = cur.fetchone() or {}
    if not row.get("line_count"):
        return None
    return row


def _has_table(cur, name):
    cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables "
                "WHERE table_schema=DATABASE() AND table_name=%s", (name,))
    return bool((cur.fetchone() or {}).get("n"))


def by_uuid(db, reship_uuid, for_update=False):
    cur = db.cursor()
    cur.execute("SELECT * FROM order_reshipments WHERE reship_uuid=%s" +
                (" FOR UPDATE" if for_update else ""), (str(reship_uuid),))
    return cur.fetchone()


def active_for_order(db, order_id, for_update=False):
    cur = db.cursor()
    cur.execute("SELECT * FROM order_reshipments WHERE order_id=%s AND status IN "
                "('RETURNED','PAYMENT_PENDING','PAID','RESHIPPED') ORDER BY id DESC LIMIT 1" +
                (" FOR UPDATE" if for_update else ""), (str(order_id),))
    return cur.fetchone()


def for_customer(db, customer_id):
    cur = db.cursor()
    cur.execute("SELECT * FROM order_reshipments WHERE customer_id=%s AND status IN "
                "('RETURNED','PAYMENT_PENDING','PAID','RESHIPPED') ORDER BY id DESC",
                (int(customer_id),))
    return {r["order_id"]: r for r in cur.fetchall()}


def shipments_for_orders(db, order_ids):
    """``{order_id: (awb, courier)}`` — the latest shipment on record for each
    order, from the courier platform's table when present."""
    ids = [str(o) for o in order_ids if o]
    if not ids:
        return {}
    cur = db.cursor()
    try:
        cur.execute("SELECT ow_order_id, tracking_number, courier FROM ops_shipping_awb "
                    "WHERE ow_order_id IN (%s) ORDER BY id" % ",".join(["%s"] * len(ids)),
                    tuple(ids))
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - table belongs to the courier platform
        return {}
    return {r["ow_order_id"]: ((r.get("tracking_number") or "").strip(),
                               (r.get("courier") or "").strip()) for r in rows}


def _latest_status(cur, order_id):
    cur.execute("SELECT order_status_name FROM order_status WHERE order_id=%s "
                "ORDER BY order_status_id DESC LIMIT 1", (str(order_id),))
    return (cur.fetchone() or {}).get("order_status_name") or ""


def courier_returning(cur, order_id):
    """The courier platform has written ``Returned`` and nothing later
    supersedes it: RETURNING_TO_OPS."""
    return _latest_status(cur, order_id) == COURIER_RETURN_STATUS


def original_shipment(cur, order_id):
    """(awb, courier) of the shipment on record for the order, from the
    courier platform's table when present."""
    try:
        cur.execute("SELECT tracking_number, courier FROM ops_shipping_awb "
                    "WHERE ow_order_id=%s ORDER BY id DESC LIMIT 1", (str(order_id),))
        row = cur.fetchone()
    except Exception:  # noqa: BLE001 - table belongs to the courier platform
        row = None
    if not row:
        return "", ""
    return (row.get("tracking_number") or "").strip(), (row.get("courier") or "").strip()


def logistics_state(row, latest_status):
    """The one label a surface shows for the returned shipment."""
    if row:
        if row["status"] == ST_RESHIPPED:
            return LOG_RESHIPPED
        if row["status"] == ST_PAID:
            return LOG_RESHIP_PAID
        if row["status"] in PAYABLE:
            return LOG_RETURNED
    if latest_status == COURIER_RETURN_STATUS:
        return LOG_RETURNING
    return None


def public_view(row, latest_status=None, open_=True, shipment=None):
    """What the customer's order card is told. No provider ids.

    ``shipment`` is the ``(awb, courier)`` of the original shipment, shown so
    the customer can follow the parcel back; the reship row's own copy wins
    once Ops has confirmed receipt."""
    state = logistics_state(row, latest_status)
    if state is None:
        return None
    awb, courier = shipment or ("", "")
    out = {"state": state, "fee": FEE_INR, "currency": CURRENCY,
           "can_pay": False, "reship_uuid": None,
           "new_awb": None, "new_courier": None, "paid_at": None, "reshipped_at": None}
    if row:
        out["reship_uuid"] = row["reship_uuid"]
        out["can_pay"] = bool(open_) and row["status"] in PAYABLE
        out["paid_at"] = row.get("paid_at")
        out["reshipped_at"] = row.get("reshipped_at")
        out["new_awb"] = row.get("new_awb")
        out["new_courier"] = row.get("new_courier")
        awb = row.get("original_awb") or awb
        courier = row.get("original_courier") or courier
    out["original_awb"] = awb or None
    out["original_courier"] = courier or None
    out["original_track_url"] = tracking_url(courier, awb)
    out["new_track_url"] = tracking_url(out["new_courier"], out["new_awb"])
    return out


# --------------------------------------------------------------------------
# Ops: confirm physical receipt
# --------------------------------------------------------------------------

def confirm_returned(db, order_id, confirmed_by, original_awb=None, courier=None,
                     return_reason=None, environ=None):
    """RETURNING_TO_OPS -> RETURNED_TO_OPS, by a named operator.

    Refuses an order that is not on the India site, whose latest status is not
    the courier's ``Returned``, or that already has an active reship. Returns
    the reship row (the existing one when the same operator retries).
    """
    ensure_schema(db)
    cur = db.cursor()
    head = _order_head(cur, order_id)
    if head is None:
        raise ReshipError("unknown_order", "Order not found", 404)
    if not is_india_host(head.get("site_from")):
        raise ReshipError("not_india", "Reship exists only for optiwar.in orders", 409)
    if not order_allowed(order_id, environ):
        raise ReshipError("not_in_rollout", "This order is outside the reship rollout "
                          "allow-list; receipt cannot be confirmed here yet", 409)
    if not courier_returning(cur, order_id):
        raise ReshipError("not_returning", "The courier has not reported this parcel "
                          "as returning; Optiwar cannot confirm receipt", 409)
    who = (confirmed_by or "").strip()
    if not who:
        raise ReshipError("operator_required", "Operator identity required")
    awb, known_courier = original_shipment(cur, order_id)
    awb = (original_awb or awb or "").strip()
    courier = (courier or known_courier or "").strip() or None

    db.commit()  # end any read snapshot before locking
    cur.execute("SELECT 1 FROM orders WHERE order_id=%s LIMIT 1 FOR UPDATE", (str(order_id),))
    existing = active_for_order(db, order_id, for_update=True)
    if existing:
        db.commit()
        return existing
    ruuid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO order_reshipments (reship_uuid, order_id, customer_id, site_from, "
        "original_awb, original_courier, return_reason, status, payment_status, "
        "fee_amount, fee_minor, fee_currency, ops_return_confirmed_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (ruuid, str(order_id), head.get("customer_id"), head.get("site_from"),
         awb, courier, (return_reason or "").strip()[:255] or None,
         ST_RETURNED, PAY_NONE, FEE_INR, FEE_MINOR, CURRENCY, who[:191]))
    add_history(cur, order_id,
                "Returned parcel received at Optiwar (AWB %s%s), confirmed by %s"
                % (awb or "n/a", (", " + courier) if courier else "", who))
    db.commit()
    row = by_uuid(db, ruuid)
    emit(db, EV_RETURNED, order_id, ruuid, row.get("customer_id"),
         {"awb": awb, "courier": courier, "by": who, "reason": return_reason})
    emit(db, EV_AVAILABLE, order_id, ruuid, row.get("customer_id"), {"fee": FEE_INR})
    return row


# --------------------------------------------------------------------------
# customer: the reship request and its payment
# --------------------------------------------------------------------------

def customer_reship(db, customer_id, order_id, host, environ=None):
    """The reship for one of this customer's orders, or 404 for anyone else's
    — and 404 too when the workflow is not open on this site/order, so .com
    never learns it exists."""
    ensure_schema(db)
    cur = db.cursor()
    head = _order_head(cur, order_id)
    if head is None or int(head.get("customer_id") or 0) != int(customer_id):
        raise ReshipError("not_found", "Not found", 404)
    if not workflow_open(host, head.get("site_from"), order_id, environ):
        raise ReshipError("not_found", "Not found", 404)
    row = active_for_order(db, order_id)
    return head, row


def begin_payment(db, customer_id, reship_uuid, host, create_order, environ=None,
                  logger=None):
    """Create — or reuse — the one Razorpay order for this reship's fee.

    Amount and currency come from this module. A second click while an order
    already exists is handed the same order id; nothing is created twice. The
    Razorpay order carries ``purpose=RESHIPMENT`` so no other path can settle
    it as merchandise.
    """
    ensure_schema(db)
    row = by_uuid(db, reship_uuid)
    if not row or int(row.get("customer_id") or 0) != int(customer_id):
        raise ReshipError("not_found", "Not found", 404)
    if not workflow_open(host, row.get("site_from"), row["order_id"], environ):
        raise ReshipError("not_found", "Not found", 404)
    if row["status"] == ST_PAID or row["status"] == ST_RESHIPPED:
        raise ReshipError("already_paid", "Reshipping charge already paid", 409)
    if row["status"] not in PAYABLE:
        raise ReshipError("not_payable", "This reship is no longer open", 409)

    db.commit()
    locked = by_uuid(db, reship_uuid, for_update=True)
    if locked["razorpay_order_id"] and locked["status"] in PAYABLE:
        db.commit()
        return locked, False
    if locked["status"] not in PAYABLE:
        db.commit()
        raise ReshipError("already_paid", "Reshipping charge already paid", 409)
    receipt = RECEIPT_PREFIX + reship_uuid.replace("-", "")[:24]
    notes = {"purpose": PURPOSE, "reship_uuid": reship_uuid,
             "original_order_id": row["order_id"], "amount": str(FEE_MINOR),
             "currency": CURRENCY, "optiwar_host": host or ""}
    try:
        rzp = create_order(FEE_MINOR, CURRENCY, receipt, notes)
    except Exception:
        db.rollback()
        raise
    if int(rzp.get("amount") or 0) != FEE_MINOR or (rzp.get("currency") or "") != CURRENCY:
        db.rollback()
        raise ReshipError("provider_mismatch", "Payment could not be started", 502)
    cur = db.cursor()
    cur.execute("UPDATE order_reshipments SET razorpay_order_id=%s, status=%s, "
                "payment_status=%s WHERE id=%s AND status IN ('RETURNED','PAYMENT_PENDING')",
                (rzp["id"], ST_PAYMENT_PENDING, PAY_PENDING, locked["id"]))
    db.commit()
    emit(db, EV_PAYMENT_STARTED, row["order_id"], reship_uuid, row.get("customer_id"),
         {"amount": FEE_MINOR, "currency": CURRENCY})
    if logger:
        logger.info("ACTIVITY:RESHIP_PAYMENT_STARTED order:%s reship:%s rzp_order:%s"
                    % (row["order_id"], reship_uuid, rzp["id"]))
    return by_uuid(db, reship_uuid), True


def is_reship_payment(payment):
    notes = (payment or {}).get("notes") or {}
    return isinstance(notes, dict) and (notes.get("purpose") or "") == PURPOSE


def reship_uuid_of(payment):
    notes = (payment or {}).get("notes") or {}
    return (notes.get("reship_uuid") or "").strip() if isinstance(notes, dict) else ""


def reship_for_payment(db, payment):
    """The reship uuid a Razorpay payment belongs to, or ''.

    Decided by the payment's Razorpay ``order_id`` against the dedicated order
    each reship stored (notes on a payment entity are whatever the checkout
    sent, so they only corroborate). Any payment this resolves must never
    reach merchandise settlement.
    """
    ensure_schema(db)
    rzp_order = ((payment or {}).get("order_id") or "").strip()
    if rzp_order:
        cur = db.cursor()
        cur.execute("SELECT reship_uuid FROM order_reshipments WHERE razorpay_order_id=%s LIMIT 1",
                    (rzp_order,))
        row = cur.fetchone()
        if row:
            return row["reship_uuid"]
    if is_reship_payment(payment):
        return reship_uuid_of(payment)
    return ""


def _payment_bound_elsewhere(cur, payment_id, reship_id):
    cur.execute("SELECT order_id FROM payment_collector WHERE payment_ref=%s "
                "AND status='TXN_SUCCESS' LIMIT 1", (payment_id,))
    row = cur.fetchone()
    if row:
        return "order " + (row.get("order_id") or "")
    cur.execute("SELECT reship_uuid FROM order_reshipments WHERE razorpay_payment_id=%s "
                "AND id<>%s LIMIT 1", (payment_id, reship_id))
    row = cur.fetchone()
    if row:
        return "reship " + (row.get("reship_uuid") or "")
    return ""


def settle_payment(db, reship_uuid, payment, source, logger=None):
    """Apply Razorpay's record of the fee payment to one reship, once.

    ``payment`` is Razorpay's payment entity, fetched or webhook-delivered —
    never the browser's claim. Returns ``{'outcome', 'reason', 'row'}``.
    Anything but APPLIED/DUPLICATE leaves the row untouched.
    """
    ensure_schema(db)
    out = {"outcome": None, "reason": "", "row": None, "reship_uuid": reship_uuid}

    def refuse(outcome, reason):
        db.rollback()
        out["outcome"], out["reason"] = outcome, reason
        if logger:
            logger.error("ACTIVITY:RESHIP_PAYMENT_REFUSED reship:%s payment:%s source:%s "
                         "outcome:%s %s" % (reship_uuid, payment.get("id", ""), source,
                                            outcome, reason))
        if outcome not in (NOT_CAPTURED, UNKNOWN_RESHIP):
            emit(db, EV_PAYMENT_REFUSED, out["row"]["order_id"] if out["row"] else "",
                 reship_uuid, None, {"outcome": outcome, "reason": reason,
                                     "payment_id": payment.get("id", "")},
                 suffix=payment.get("id", ""))
        return out

    payment_id = (payment.get("id") or "").strip()
    if not payment_id:
        return refuse(UNKNOWN_RESHIP, "no payment id")
    if (payment.get("status") or "") != "captured":
        return refuse(NOT_CAPTURED, "status %s" % payment.get("status"))

    row = by_uuid(db, reship_uuid, for_update=True)
    out["row"] = row
    if not row:
        return refuse(UNKNOWN_RESHIP, "no such reship")
    if row["razorpay_payment_id"] == payment_id and row["status"] in (ST_PAID, ST_RESHIPPED):
        db.commit()
        out["outcome"] = DUPLICATE
        return out
    if (payment.get("order_id") or "") != (row["razorpay_order_id"] or "~"):
        return refuse(ORDER_MISMATCH, "payment is for razorpay order %s, reship holds %s"
                      % (payment.get("order_id"), row["razorpay_order_id"]))
    if reship_uuid_of(payment) and reship_uuid_of(payment) != reship_uuid:
        return refuse(ORDER_MISMATCH, "payment notes name another reship")
    if int(payment.get("amount") or 0) != int(row["fee_minor"]):
        return refuse(AMOUNT_MISMATCH, "paid %s, fee %s" % (payment.get("amount"),
                                                             row["fee_minor"]))
    if (payment.get("currency") or "") != row["fee_currency"]:
        return refuse(CURRENCY_MISMATCH, "paid in %s, fee in %s"
                      % (payment.get("currency"), row["fee_currency"]))
    cur = db.cursor()
    elsewhere = _payment_bound_elsewhere(cur, payment_id, row["id"])
    if elsewhere:
        return refuse(ALREADY_BOUND, "payment already paid " + elsewhere)
    if row["status"] not in PAYABLE:
        return refuse(NOT_PAYABLE, "reship is %s" % row["status"])

    dump = json.dumps({k: payment.get(k) for k in
                       ("id", "order_id", "amount", "currency", "status", "method",
                        "created_at")}, default=str)
    try:
        cur.execute("UPDATE order_reshipments SET razorpay_payment_id=%s, payment_dump=%s, "
                    "status=%s, payment_status=%s, paid_at=NOW(), paid_source=%s "
                    "WHERE id=%s AND status IN ('RETURNED','PAYMENT_PENDING')",
                    (payment_id, dump, ST_PAID, PAY_PAID, source, row["id"]))
        if cur.rowcount != 1:
            db.rollback()
            out["outcome"] = DUPLICATE
            return out
        add_history(cur, row["order_id"],
                    "Reshipping charge INR %d received - razorpay %s" % (FEE_INR, payment_id))
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        if "Duplicate entry" in str(exc) or (exc.args and exc.args[0] in (1062, 1586)):
            return refuse(ALREADY_BOUND, "payment id already bound")
        raise
    emit(db, EV_PAYMENT_COMPLETED, row["order_id"], reship_uuid, row.get("customer_id"),
         {"payment_id": payment_id, "amount": FEE_MINOR, "currency": CURRENCY,
          "source": source})
    if logger:
        logger.info("ACTIVITY:RESHIP_PAID order:%s reship:%s payment:%s source:%s"
                    % (row["order_id"], reship_uuid, payment_id, source))
    out["outcome"] = APPLIED
    out["row"] = by_uuid(db, reship_uuid)
    return out


# --------------------------------------------------------------------------
# Ops: ship again
# --------------------------------------------------------------------------

def ship(db, reship_uuid, shipped_by, new_awb, new_courier):
    """PAID -> RESHIPPED with a new AWB. The original AWB stays where it is;
    an unpaid reship is refused."""
    ensure_schema(db)
    who = (shipped_by or "").strip()
    awb = (new_awb or "").strip()
    courier = (new_courier or "").strip()
    if not who:
        raise ReshipError("operator_required", "Operator identity required")
    if not awb or not courier:
        raise ReshipError("awb_required", "Courier and new AWB are required")
    row = by_uuid(db, reship_uuid, for_update=True)
    if not row:
        db.rollback()
        raise ReshipError("not_found", "Not found", 404)
    if row["status"] == ST_RESHIPPED:
        db.commit()
        if row["new_awb"] == awb:
            return row
        raise ReshipError("already_shipped", "Already reshipped under AWB %s"
                          % row["new_awb"], 409)
    if row["status"] != ST_PAID or row["payment_status"] != PAY_PAID:
        db.rollback()
        raise ReshipError("unpaid", "Reshipping charge not paid; cannot ship", 409)
    if awb == (row["original_awb"] or ""):
        db.rollback()
        raise ReshipError("same_awb", "The new AWB must differ from the original", 400)
    cur = db.cursor()
    cur.execute("UPDATE order_reshipments SET status=%s, new_awb=%s, new_courier=%s, "
                "shipped_by=%s, reshipped_at=NOW() WHERE id=%s AND status='PAID'",
                (ST_RESHIPPED, awb[:64], courier[:64], who[:191], row["id"]))
    if cur.rowcount != 1:
        db.rollback()
        raise ReshipError("conflict", "Reship changed underneath; reload", 409)
    # A genuinely new shipment: a fresh Shipped row, appended, nothing rewritten.
    cur.execute("INSERT INTO order_status (order_status_name, order_id) VALUES (%s,%s)",
                ("Shipped", row["order_id"]))
    try:
        cur.execute("UPDATE order_status SET source=%s, note=%s WHERE order_status_id=%s",
                    ("reship", "reshipped %s %s by %s" % (courier, awb, who)[:255],
                     cur.lastrowid))
    except Exception:  # noqa: BLE001 - provenance columns are optional
        pass
    add_history(cur, row["order_id"], "Reshipped via %s - AWB %s (original AWB %s kept)"
                % (courier, awb, row["original_awb"] or "n/a"))
    if _has_table(cur, "ops_shipping_awb"):
        # The courier platform's own shipment table: a new row, the original
        # AWB row untouched. When the table exists the row must land.
        cur.execute("INSERT INTO ops_shipping_awb (ow_order_id, tracking_number, courier, "
                    "awb_status, created_by) VALUES (%s,%s,%s,'created',%s)",
                    (row["order_id"], awb, courier, ("reship:" + who)[:100]))
    db.commit()
    emit(db, EV_SHIPPED, row["order_id"], reship_uuid, row.get("customer_id"),
         {"awb": awb, "courier": courier, "by": who})
    return by_uuid(db, reship_uuid)


# --------------------------------------------------------------------------
# Ops queue
# --------------------------------------------------------------------------

def ops_queue(db, days=60, environ=None):
    """India orders the courier reports returning, and every active reship,
    for the Ops page. Newest first. Orders outside the rollout allow-list are
    left out, so Ops is never offered a confirmation the workflow would refuse."""
    ensure_schema(db)
    cur = db.cursor()
    cur.execute(
        "SELECT os.order_id, MAX(os.order_status_id) AS sid FROM order_status os "
        "WHERE os.order_status_name=%s AND os.order_status_id IN ("
        "  SELECT MAX(order_status_id) FROM order_status GROUP BY order_id) "
        "GROUP BY os.order_id ORDER BY sid DESC LIMIT 200", (COURIER_RETURN_STATUS,))
    returning = []
    for r in cur.fetchall():
        if not order_allowed(r["order_id"], environ):
            continue
        head = _order_head(cur, r["order_id"])
        if not head or not is_india_host(head.get("site_from")):
            continue
        awb, courier = original_shipment(cur, r["order_id"])
        returning.append({"order_id": r["order_id"], "awb": awb, "courier": courier,
                          "customer_id": head.get("customer_id"),
                          "track_url": tracking_url(courier, awb)})
    cur.execute("SELECT * FROM order_reshipments WHERE status IN "
                "('RETURNED','PAYMENT_PENDING','PAID','RESHIPPED') "
                "AND created_at >= NOW() - INTERVAL %s DAY ORDER BY id DESC", (int(days),))
    active = cur.fetchall()
    active_ids = {a["order_id"] for a in active}
    return {"returning": [r for r in returning if r["order_id"] not in active_ids],
            "active": active}


# --------------------------------------------------------------------------
# reconcile: a paid fee whose callback never arrived
# --------------------------------------------------------------------------

def reconcile_pending_payments(db, fetch_order_payments, logger=None, max_age_hours=72):
    """For every reship with a Razorpay order and no payment, ask Razorpay.
    A captured payment goes through ``settle_payment`` like any other; a
    provider failure is skipped and retried next run."""
    ensure_schema(db)
    cur = db.cursor()
    cur.execute("SELECT reship_uuid, razorpay_order_id, order_id FROM order_reshipments "
                "WHERE status='PAYMENT_PENDING' AND razorpay_order_id IS NOT NULL "
                "AND updated_at >= NOW() - INTERVAL %s HOUR", (int(max_age_hours),))
    rows = cur.fetchall()
    summary = {"checked": len(rows), "settled": [], "unpaid": 0, "refused": [],
               "unavailable": 0}
    for r in rows:
        try:
            payments = fetch_order_payments(r["razorpay_order_id"]) or []
        except Exception as exc:  # noqa: BLE001
            summary["unavailable"] += 1
            if logger:
                logger.warning("RESHIP_RECONCILE_UNAVAILABLE reship:%s %s"
                               % (r["reship_uuid"], str(exc)[:120]))
            continue
        captured = [p for p in payments if (p.get("status") or "") == "captured"]
        if not captured:
            summary["unpaid"] += 1
            continue
        res = settle_payment(db, r["reship_uuid"], captured[0], "razorpay-reconcile",
                             logger=logger)
        if res["outcome"] == APPLIED:
            summary["settled"].append(r["reship_uuid"])
        elif res["outcome"] != DUPLICATE:
            summary["refused"].append({"reship_uuid": r["reship_uuid"],
                                       "outcome": res["outcome"]})
    return summary


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------

# WhatsApp template bodies as they will be submitted to MSG91/Meta (Utility,
# en) once the owner approves the wording. Until RESHIP_WA_TEMPLATES_APPROVED
# is set, only the email channel is used.
WA_TEMPLATES = {
    "return_started": {
        "header": "Optiwar Delivery Update",
        "body": ("Your package for order {{1}} is being returned to Optiwar because "
                 "delivery could not be completed.\n\n"
                 "You will be able to reschedule delivery once the package reaches "
                 "back to us. We will notify you when it is ready for reshipping.\n\n"
                 "No payment is required at this stage."),
        "variables": {"1": "order id"},
    },
    "reship_available": {
        "header": "Optiwar — Your Package Has Returned",
        "body": ("Your package for order {{1}} has now reached back to Optiwar.\n\n"
                 "You can reship your order by paying the ₹250 reshipping charge. "
                 "Open My Orders to continue:\n{{2}}\n\n"
                 "Prescription/customized lenses are not eligible for cancellation "
                 "after preparation.\n\n"
                 "Safety notice: Make payments only through Optiwar's official "
                 "website. Optiwar will never ask you to share OTPs, passwords, card "
                 "details or banking credentials over WhatsApp."),
        "variables": {"1": "order id", "2": "My Orders URL"},
    },
    "reship_paid": {
        "header": "Optiwar — Reshipping Confirmed",
        "body": ("We have received your ₹250 reshipping charge for order {{1}}.\n\n"
                 "Your package will now be prepared for reshipment. We will send the "
                 "new tracking details after dispatch."),
        "variables": {"1": "order id"},
    },
}

EMAILS = {
    EV_RETURN_STARTED: (
        "Optiwar — delivery update for order {order_id}",
        "Hello {name},\n\nYour package for order {order_id} is being returned to "
        "Optiwar because delivery could not be completed.\n\nYou will be able to "
        "reschedule delivery once the package reaches back to us. We will notify you "
        "when it is ready for reshipping.\n\nNo payment is required at this stage.\n\n"
        "Regards,\nOptiwar Support\n"),
    EV_AVAILABLE: (
        "Optiwar — your package has returned (order {order_id})",
        "Hello {name},\n\nYour package for order {order_id} has now reached back to "
        "Optiwar.\n\nYou can reship your order by paying the Rs 250 reshipping charge. "
        "Open My Orders to continue:\n{url}\n\nPrescription/customized lenses are not "
        "eligible for cancellation after preparation.\n\nSafety notice: make payments "
        "only through Optiwar's official website. Optiwar will never ask you to share "
        "OTPs, passwords, card details or banking credentials.\n\nRegards,\n"
        "Optiwar Support\n"),
    EV_PAYMENT_COMPLETED: (
        "Optiwar — reshipping confirmed for order {order_id}",
        "Hello {name},\n\nWe have received your Rs 250 reshipping charge for order "
        "{order_id}.\n\nYour package will now be prepared for reshipment. We will send "
        "the new tracking details after dispatch.\n\nRegards,\nOptiwar Support\n"),
}
_WA_FOR_EVENT = {EV_RETURN_STARTED: "return_started", EV_AVAILABLE: "reship_available",
                 EV_PAYMENT_COMPLETED: "reship_paid"}


def my_orders_url(host):
    host = (host or "optiwar.in").strip()
    if not host.startswith("http"):
        host = "https://" + host
    return host.rstrip("/") + "/profile/?tab=orders"


def _account(cur, customer_id):
    if not customer_id:
        return {}
    cur.execute("SELECT customer_name, customer_email, customer_phone FROM customers "
                "WHERE customer_id=%s LIMIT 1", (int(customer_id),))
    return cur.fetchone() or {}


def _html(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notify(db, event_type, order_id, customer_id, host, reship_uuid=None,
           mailer=None, whatsapp=None, environ=None, key=None):
    """Send the customer the message for one logical event, once per channel.

    The claim is the ``reship.notified``/``notify_failed`` event id derived
    from (event, key, channel): a second call for the same event finds it and
    sends nothing. A failed delivery is recorded and never changes the reship.
    """
    env = os.environ if environ is None else environ
    if event_type not in EMAILS:
        return {"sent": False, "reason": "no_template"}
    cur = db.cursor()
    acct = _account(cur, customer_id)
    name = (acct.get("customer_name") or "").strip() or "Customer"
    fields = {"name": name, "order_id": str(order_id), "url": my_orders_url(host)}
    claim_key = key or reship_uuid or str(order_id)
    out = {"sent": False, "email": None, "whatsapp": None}

    email = (acct.get("customer_email") or "").strip()
    if email and "@" in email:
        claimed = emit(db, EV_NOTIFIED, order_id, reship_uuid, customer_id,
                       {"event": event_type, "channel": "email"},
                       key=claim_key, suffix="%s:email" % event_type)
        if claimed:
            subject, text = EMAILS[event_type]
            try:
                if mailer is None:
                    mailer = _default_mailer
                mailer(email, subject.format(**fields), text.format(**fields))
                out["email"] = "SENT"
                out["sent"] = True
            except Exception as exc:  # noqa: BLE001
                out["email"] = "FAILED"
                emit(db, EV_NOTIFY_FAILED, order_id, reship_uuid, customer_id,
                     {"event": event_type, "channel": "email", "error": str(exc)[:160]},
                     key=claim_key, suffix="%s:email:fail" % event_type)

    phone = (acct.get("customer_phone") or "").strip()
    approved = str(env.get(WA_APPROVED_ENV, "")).strip().lower() in ("1", "true", "yes")
    if phone and approved:
        claimed = emit(db, EV_NOTIFIED, order_id, reship_uuid, customer_id,
                       {"event": event_type, "channel": "whatsapp"},
                       key=claim_key, suffix="%s:whatsapp" % event_type)
        if claimed:
            tpl = env.get(WA_TEMPLATE_PREFIX_ENV, "") + _WA_FOR_EVENT[event_type]
            comps = {"body_1": {"type": "text", "value": str(order_id)}}
            if event_type == EV_AVAILABLE:
                comps["body_2"] = {"type": "text", "value": fields["url"]}
            try:
                if whatsapp is None:
                    whatsapp = _default_whatsapp
                r = whatsapp(phone.replace("+", "").replace(" ", "").replace("-", ""),
                             tpl, comps) or {}
                ok = bool(r.get("ok"))
            except Exception as exc:  # noqa: BLE001
                ok = False
                r = {"error": str(exc)[:160]}
            out["whatsapp"] = "SENT" if ok else "FAILED"
            out["sent"] = out["sent"] or ok
            if not ok:
                emit(db, EV_NOTIFY_FAILED, order_id, reship_uuid, customer_id,
                     {"event": event_type, "channel": "whatsapp", "error": r.get("error")},
                     key=claim_key, suffix="%s:whatsapp:fail" % event_type)
    return out


def notify_shipped(db, row, host, notify_order_shipped, environ=None):
    """The existing approved ``order_shipped`` message, once per reship."""
    if not emit(db, EV_NOTIFIED, row["order_id"], row["reship_uuid"], row.get("customer_id"),
                {"event": EV_SHIPPED, "channel": "all"}, suffix="%s:all" % EV_SHIPPED):
        return {"sent": False, "reason": "duplicate"}
    acct = _account(db.cursor(), row.get("customer_id"))
    try:
        notify_order_shipped(acct.get("customer_email"), acct.get("customer_phone"),
                             acct.get("customer_name"), row["order_id"], host,
                             tracking_info="%s %s" % (row["new_courier"], row["new_awb"]))
        return {"sent": True}
    except Exception as exc:  # noqa: BLE001
        emit(db, EV_NOTIFY_FAILED, row["order_id"], row["reship_uuid"], row.get("customer_id"),
             {"event": EV_SHIPPED, "error": str(exc)[:160]}, suffix="%s:all:fail" % EV_SHIPPED)
        return {"sent": False, "reason": "failed"}


def sweep_return_started(db, host_for=None, mailer=None, whatsapp=None, environ=None,
                         max_age_days=30):
    """Tell each customer once that the courier is returning their parcel.

    The courier platform writes ``Returned`` straight into ``order_status``,
    outside this application, so the event is picked up here: every India
    order whose latest status is ``Returned`` gets ``shipment.return_started``
    keyed by that status row, and the notification is claimed under it.
    """
    ensure_schema(db)
    if not enabled(environ):
        return {"notified": 0, "skipped": "disabled"}
    cur = db.cursor()
    cur.execute(
        "SELECT os.order_id, os.order_status_id FROM order_status os "
        "WHERE os.order_status_name=%s "
        "AND os.order_status_id IN (SELECT MAX(order_status_id) FROM order_status GROUP BY order_id) "
        "AND (os.created_at IS NULL OR os.created_at >= NOW() - INTERVAL %s DAY) "
        "ORDER BY os.order_status_id DESC LIMIT 200",
        (COURIER_RETURN_STATUS, int(max_age_days)))
    count = 0
    for r in cur.fetchall():
        head = _order_head(cur, r["order_id"])
        if not head or head.get("is_test") or not is_india_host(head.get("site_from")):
            continue
        if not order_allowed(r["order_id"], environ):
            continue
        key = "%s:%s" % (r["order_id"], r["order_status_id"])
        if not emit(db, EV_RETURN_STARTED, r["order_id"], None, head.get("customer_id"),
                    {"status_id": r["order_status_id"]}, key=key):
            continue
        host = host_for(head.get("site_from")) if host_for else head.get("site_from")
        res = notify(db, EV_RETURN_STARTED, r["order_id"], head.get("customer_id"), host,
                     mailer=mailer, whatsapp=whatsapp, environ=environ, key=key)
        if res.get("sent"):
            count += 1
    return {"notified": count}


def _default_mailer(to_email, subject, text):
    from flask import current_app
    from flask_mail import Message
    msg = Message(subject=subject, recipients=[to_email], body=text,
                  html="<pre style='font-family:inherit;white-space:pre-wrap'>%s</pre>" % _html(text),
                  sender="Optiwar Support <support@optiwar.com>",
                  reply_to="support@optiwar.com")
    current_app.extensions["mail"].send(msg)


def _default_whatsapp(phone, template, components):
    try:
        from .notifications import send_whatsapp_tracked
    except ImportError:
        from notifications import send_whatsapp_tracked
    return send_whatsapp_tracked(phone, template, components)
