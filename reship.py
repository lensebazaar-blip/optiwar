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
                                 \-> ABANDONED   (held ``RESHIP_ABANDON_AFTER_DAYS``,
                                                  default 60, unpaid the whole time)

The holding period runs from ``returned_at`` — the moment Ops confirmed
physical possession — and from nothing the courier says. ``abandon_at`` is
fixed on that row at confirmation, so a later change of the configured period
never moves a deadline a customer was already told. Reminders on the configured
days and the abandonment itself are the work of ``sweep_holding`` (the 10-minute
reconcile worker), each claimed once under a deterministic event id. Abandonment
locks the row and re-reads it: a captured payment that settled first wins, a
held row waits, and a PAID or RESHIPPED row is never abandoned. Ops learns of an
abandonment from the queue API and, when ``RESHIP_OPS_WEBHOOK_URL`` is set,
from a signed ``reship.abandoned`` POST.

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
import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from urllib import request as urlrequest
from urllib.parse import quote

try:
    from .paid_orders import add_history
except ImportError:  # loaded as a plain module (tests, deploy tool)
    from paid_orders import add_history

ENABLED_ENV = "RESHIP_ENABLED_IN"
ALLOW_ORDERS_ENV = "RESHIP_ALLOW_ORDERS"          # optional: comma list, first rollout
WA_APPROVED_ENV = "RESHIP_WA_TEMPLATES_APPROVED"   # WhatsApp only once Meta approved
WA_TEMPLATE_PREFIX_ENV = "RESHIP_WA_TEMPLATE_PREFIX"
WA_REMINDERS_APPROVED_ENV = "RESHIP_WA_REMINDER_TEMPLATES_APPROVED"  # the 3 holding templates
ABANDON_DAYS_ENV = "RESHIP_ABANDON_AFTER_DAYS"
REMINDER_DAYS_ENV = "RESHIP_REMINDER_DAYS"
FINAL_WINDOW_ENV = "RESHIP_FINAL_WINDOW_DAYS"
OPS_WEBHOOK_URL_ENV = "RESHIP_OPS_WEBHOOK_URL"
OPS_WEBHOOK_SECRET_ENV = "RESHIP_OPS_WEBHOOK_SECRET"

DEFAULT_ABANDON_DAYS = 60
DEFAULT_REMINDER_DAYS = (30, 45, 55)
DEFAULT_FINAL_WINDOW_DAYS = 5
ABANDON_REASON = "RETURNED_UNCLAIMED_%d_DAYS"
SUPPORT_EMAIL = "support@optiwar.com"

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
ST_ABANDONED = "ABANDONED"          # terminal: held the whole period, never paid
ACTIVE = (ST_RETURNED, ST_PAYMENT_PENDING, ST_PAID, ST_RESHIPPED)
PAYABLE = (ST_RETURNED, ST_PAYMENT_PENDING)
SHOWN = ACTIVE + (ST_ABANDONED,)     # the customer's card and the Ops queue
_SHOWN_SQL = "('RETURNED','PAYMENT_PENDING','PAID','RESHIPPED','ABANDONED')"

# Ops synchronisation of an abandonment: the queue API always carries it;
# a configured webhook is SENT, PENDING (not yet tried) or FAILED (retried).
SYNC_QUEUE = "QUEUE"
SYNC_PENDING = "PENDING"
SYNC_SENT = "SENT"
SYNC_FAILED = "FAILED"

# payment sub-state
PAY_NONE = "NONE"
PAY_PENDING = "PENDING"
PAY_PAID = "PAID"

# logistics state a customer or Ops is shown
LOG_RETURNING = "RETURNING_TO_OPS"
LOG_RETURNED = "RETURNED_TO_OPS"
LOG_RESHIP_PAID = "RESHIP_PAID"
LOG_RESHIPPED = "RESHIPPED"
LOG_ABANDONED = "ABANDONED"

# Derived Ops states (never persisted): what the queue row means right now.
OPS_RETURNED = "RETURNED_TO_OPS"
OPS_NOTIFIED = "CUSTOMER_NOTIFIED"
OPS_AWAITING_PAYMENT = "AWAITING_PAYMENT"
OPS_READY = "READY_TO_RESHIP"
OPS_RESHIPPED = "RESHIPPED"
OPS_PENDING_ABANDON = "ABANDONMENT_PENDING"
OPS_ABANDONED = "ABANDONED"
OPS_HELD = "ON_HOLD"

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
EV_REMINDER = "reship.reminder"
EV_FINAL_WARNING = "reship.final_warning"
EV_ABANDONED = "reship.abandoned"
EV_HOLD = "reship.hold"
EV_HOLD_RELEASED = "reship.hold_released"
EV_OPS_SYNC = "reship.ops_sync"
EV_OPS_SYNC_FAILED = "reship.ops_sync_failed"

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
    returned_at              DATETIME NULL,
    abandon_at               DATETIME NULL,
    abandoned_at             DATETIME NULL,
    abandon_reason           VARCHAR(64) NULL,
    hold_reason              VARCHAR(255) NULL,
    hold_by                  VARCHAR(191) NULL,
    hold_at                  DATETIME NULL,
    ops_sync_status          VARCHAR(16) NULL,
    ops_synced_at            DATETIME NULL,
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

# Columns added after the table first shipped. Nullable, so the release before
# this one keeps inserting; the deploy tool applies them as a deliberate step
# and ensure_schema adds whichever a box (test DB) still lacks.
HOLDING_COLUMNS = (
    ("returned_at", "DATETIME NULL"),
    ("abandon_at", "DATETIME NULL"),
    ("abandoned_at", "DATETIME NULL"),
    ("abandon_reason", "VARCHAR(64) NULL"),
    ("hold_reason", "VARCHAR(255) NULL"),
    ("hold_by", "VARCHAR(191) NULL"),
    ("hold_at", "DATETIME NULL"),
    ("ops_sync_status", "VARCHAR(16) NULL"),
    ("ops_synced_at", "DATETIME NULL"),
)
ADDED_COLUMNS = (("order_reshipments", HOLDING_COLUMNS),)

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
    for table, columns in ADDED_COLUMNS:
        cur.execute("SELECT column_name AS column_name FROM information_schema.columns "
                    "WHERE table_schema=DATABASE() AND table_name=%s", (table,))
        have = {r["column_name"].lower() for r in cur.fetchall()}
        for name, decl in columns:
            if name.lower() not in have:
                cur.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
    # Rows confirmed before the holding period existed: their clock starts at
    # the receipt they already recorded, under the period configured now.
    cur.execute("UPDATE order_reshipments SET returned_at=ops_return_confirmed_at, "
                "abandon_at=ops_return_confirmed_at + INTERVAL %s DAY "
                "WHERE returned_at IS NULL", (abandon_days(),))
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


def _int_env(env, name, default, low=1):
    try:
        return max(low, int(str(env.get(name, "")).strip() or default))
    except ValueError:
        return default


def abandon_days(environ=None):
    """How long a returned parcel is held for the customer, in days."""
    env = os.environ if environ is None else environ
    return _int_env(env, ABANDON_DAYS_ENV, DEFAULT_ABANDON_DAYS)


def reminder_days(environ=None):
    """The days after receipt on which the customer is reminded — ascending,
    each strictly inside the holding period."""
    env = os.environ if environ is None else environ
    raw = str(env.get(REMINDER_DAYS_ENV, "")).strip()
    days = set()
    for part in raw.split(",") if raw else []:
        try:
            days.add(int(part.strip()))
        except ValueError:
            continue
    if not raw:
        days = set(DEFAULT_REMINDER_DAYS)
    limit = abandon_days(environ)
    return tuple(sorted(d for d in days if 0 < d < limit))


def final_window_days(environ=None):
    """Within this many days of the deadline the card says FINAL and the
    Ops state is ABANDONMENT_PENDING."""
    env = os.environ if environ is None else environ
    return _int_env(env, FINAL_WINDOW_ENV, DEFAULT_FINAL_WINDOW_DAYS)


def order_allowed(order_id, environ=None):
    """The optional first-rollout allow-list: empty means every .in order."""
    env = os.environ if environ is None else environ
    allow = [o.strip() for o in str(env.get(ALLOW_ORDERS_ENV, "")).split(",") if o.strip()]
    return not allow or str(order_id) in allow


TRACKING_PAGES = {
    "dtdc": "https://www.dtdc.com/track",
    "delhivery": "https://www.delhivery.com/track-v2/package/{awb}",
}


AWB_FORMATS = {
    "dtdc": (re.compile(r"^[A-Z0-9]{9,14}$"), "a DTDC AWB is 9-14 letters/digits, e.g. 7X119057819"),
    "delhivery": (re.compile(r"^[0-9]{10,16}$"), "a Delhivery AWB is 10-16 digits"),
}


def awb_format_error(courier, awb):
    """Why an AWB cannot belong to the named courier, or None when it can
    (or the courier's format is unknown to us)."""
    rule = AWB_FORMATS.get((courier or "").strip().lower())
    if not rule:
        return None
    pattern, hint = rule
    return None if pattern.match((awb or "").strip().upper()) else hint


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
    """The order's current reship record — an ABANDONED one included, since a
    parcel abandoned is not a parcel that can be received again."""
    cur = db.cursor()
    cur.execute("SELECT * FROM order_reshipments WHERE order_id=%s AND status IN " +
                _SHOWN_SQL + " ORDER BY id DESC LIMIT 1" +
                (" FOR UPDATE" if for_update else ""), (str(order_id),))
    return cur.fetchone()


def for_customer(db, customer_id):
    cur = db.cursor()
    cur.execute("SELECT * FROM order_reshipments WHERE customer_id=%s AND status IN " +
                _SHOWN_SQL + " ORDER BY id DESC", (int(customer_id),))
    return {r["order_id"]: r for r in cur.fetchall()}


def db_now(db):
    """The database clock — the one ``returned_at`` and ``abandon_at`` were
    written by, so the countdown and the sweep agree with the row."""
    cur = db.cursor()
    cur.execute("SELECT NOW() AS now")
    return cur.fetchone()["now"]


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


def held(row):
    return bool(row and row.get("hold_reason"))


def holding(row, now=None, environ=None):
    """The server's reading of the holding period for one row.

    ``days_remaining`` counts whole days left until ``abandon_at`` (0 on the
    deadline day, never negative); ``final_period`` is the last
    ``RESHIP_FINAL_WINDOW_DAYS`` of it. None-valued for rows without a
    deadline (never confirmed, or written before the period existed and not
    yet backfilled).
    """
    out = {"returned_at": None, "abandon_at": None, "days_remaining": None,
           "days_held": None, "final_period": False, "on_hold": False,
           "abandoned_at": None, "holding_days": abandon_days(environ)}
    if not row:
        return out
    returned_at = row.get("returned_at") or row.get("ops_return_confirmed_at")
    abandon_at = row.get("abandon_at")
    out["returned_at"], out["abandon_at"] = returned_at, abandon_at
    out["abandoned_at"] = row.get("abandoned_at")
    out["on_hold"] = held(row)
    if returned_at and abandon_at:
        out["holding_days"] = (abandon_at - returned_at).days
    now = now or datetime.now()
    if returned_at:
        out["days_held"] = max(0, (now - returned_at).days)
    if abandon_at and row["status"] in PAYABLE:
        left = (abandon_at - now).total_seconds()
        out["days_remaining"] = int(-(-left // 86400)) if left > 0 else 0
        out["final_period"] = out["days_remaining"] <= final_window_days(environ)
    return out


def ops_state(row, notified_day0=False, now=None, environ=None):
    """The derived Ops queue state for one row (never stored)."""
    st = row["status"]
    if st == ST_ABANDONED:
        return OPS_ABANDONED
    if st == ST_RESHIPPED:
        return OPS_RESHIPPED
    if st == ST_PAID:
        return OPS_READY
    if held(row):
        return OPS_HELD
    h = holding(row, now, environ)
    if h["final_period"] and h["days_remaining"] is not None:
        return OPS_PENDING_ABANDON
    if st == ST_PAYMENT_PENDING:
        return OPS_AWAITING_PAYMENT
    return OPS_NOTIFIED if notified_day0 else OPS_RETURNED


def logistics_state(row, latest_status):
    """The one label a surface shows for the returned shipment."""
    if row:
        if row["status"] == ST_ABANDONED:
            return LOG_ABANDONED
        if row["status"] == ST_RESHIPPED:
            return LOG_RESHIPPED
        if row["status"] == ST_PAID:
            return LOG_RESHIP_PAID
        if row["status"] in PAYABLE:
            return LOG_RETURNED
    if latest_status == COURIER_RETURN_STATUS:
        return LOG_RETURNING
    return None


def public_view(row, latest_status=None, open_=True, shipment=None, now=None, environ=None):
    """What the customer's order card is told. No provider ids.

    ``shipment`` is the ``(awb, courier)`` of the original shipment, shown so
    the customer can follow the parcel back; the reship row's own copy wins
    once Ops has confirmed receipt. The deadline and the days left are the
    server's (``holding``); the browser computes nothing."""
    state = logistics_state(row, latest_status)
    if state is None:
        return None
    awb, courier = shipment or ("", "")
    out = {"state": state, "fee": FEE_INR, "currency": CURRENCY,
           "can_pay": False, "reship_uuid": None,
           "new_awb": None, "new_courier": None, "paid_at": None, "reshipped_at": None,
           "returned_at": None, "abandon_at": None, "days_remaining": None,
           "holding_days": abandon_days(environ), "final_period": False,
           "abandoned_at": None, "support_email": SUPPORT_EMAIL}
    if row:
        h = holding(row, now, environ)
        for k in ("returned_at", "abandon_at", "days_remaining", "holding_days",
                  "final_period", "abandoned_at"):
            out[k] = h[k]
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
        "fee_amount, fee_minor, fee_currency, ops_return_confirmed_by, "
        "ops_return_confirmed_at, returned_at, abandon_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),NOW() + INTERVAL %s DAY)",
        (ruuid, str(order_id), head.get("customer_id"), head.get("site_from"),
         awb, courier, (return_reason or "").strip()[:255] or None,
         ST_RETURNED, PAY_NONE, FEE_INR, FEE_MINOR, CURRENCY, who[:191],
         abandon_days(environ)))
    add_history(cur, order_id,
                "Returned parcel received at Optiwar (AWB %s%s), confirmed by %s; "
                "held %d days for reshipment"
                % (awb or "n/a", (", " + courier) if courier else "", who,
                   abandon_days(environ)))
    db.commit()
    row = by_uuid(db, ruuid)
    emit(db, EV_RETURNED, order_id, ruuid, row.get("customer_id"),
         {"awb": awb, "courier": courier, "by": who, "reason": return_reason,
          "returned_at": row.get("returned_at"), "abandon_at": row.get("abandon_at")})
    emit(db, EV_AVAILABLE, order_id, ruuid, row.get("customer_id"),
         {"fee": FEE_INR, "abandon_at": row.get("abandon_at")})
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
    bad = awb_format_error(courier, awb)
    if bad:
        db.rollback()
        raise ReshipError("awb_format", "AWB %s does not look like a %s number: %s"
                          % (awb, courier, bad), 400)
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
        # AWB row untouched. The platform books the AWB itself and may have
        # written its row already; then there is nothing to add.
        cur.execute("SELECT 1 FROM ops_shipping_awb WHERE ow_order_id=%s AND tracking_number=%s "
                    "LIMIT 1", (row["order_id"], awb))
        if not cur.fetchone():
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
    # Open rows always; a finished one (RESHIPPED, ABANDONED) for ``days`` after
    # it finished — not after it was created, or a row would leave the queue on
    # the very day it was abandoned.
    cur.execute("SELECT * FROM order_reshipments WHERE status IN " + _SHOWN_SQL +
                " AND (status IN ('RETURNED','PAYMENT_PENDING','PAID') OR "
                "COALESCE(reshipped_at, abandoned_at, created_at) >= NOW() - INTERVAL %s DAY) "
                "ORDER BY id DESC", (int(days),))
    active = cur.fetchall()
    now = db_now(db)
    notes = notifications_for(db, [a["reship_uuid"] for a in active])
    for a in active:
        a.update(ops_projection(a, notes.get(a["reship_uuid"], []), now, environ))
    active_ids = {a["order_id"] for a in active}
    return {"returning": [r for r in returning if r["order_id"] not in active_ids],
            "active": active}


def notifications_for(db, reship_uuids):
    """``{reship_uuid: [(event, channel, created_at, failed)]}`` — every
    customer notification claimed for these rows, in order."""
    ids = [str(u) for u in reship_uuids if u]
    if not ids:
        return {}
    cur = db.cursor()
    cur.execute("SELECT reship_uuid, event_type, payload, created_at FROM reship_events "
                "WHERE event_type IN ('reship.notified','reship.notify_failed') "
                "AND reship_uuid IN (%s) ORDER BY id" % ",".join(["%s"] * len(ids)), tuple(ids))
    out = {}
    for r in cur.fetchall():
        try:
            p = json.loads(r.get("payload") or "{}")
        except ValueError:
            p = {}
        out.setdefault(r["reship_uuid"], []).append(
            (p.get("event"), p.get("channel"), r["created_at"],
             r["event_type"] == EV_NOTIFY_FAILED))
    return out


def ops_projection(row, notes, now=None, environ=None):
    """The authoritative fields Ops reads instead of computing: deadline,
    days left, derived state, notification and payment state, hold, sync."""
    sent = [n for n in notes if not n[3]]
    failed = [n for n in notes if n[3]]
    day0 = any(n[0] == EV_AVAILABLE for n in sent)
    h = holding(row, now, environ)
    last = sent[-1] if sent else None
    if row["status"] == ST_PAID:
        pay = "PAID"
    elif row["status"] == ST_RESHIPPED:
        pay = "PAID"
    elif row["status"] == ST_ABANDONED:
        pay = "NEVER_PAID"
    elif row["status"] == ST_PAYMENT_PENDING:
        pay = "PAYMENT_STARTED"
    else:
        pay = "AWAITING_PAYMENT"
    return {
        "ops_state": ops_state(row, day0, now, environ),
        "returned_at": h["returned_at"], "abandon_at": h["abandon_at"],
        "days_remaining": h["days_remaining"], "days_held": h["days_held"],
        "final_period": h["final_period"], "holding_days": h["holding_days"],
        "on_hold": h["on_hold"],
        "payment_state": pay,
        "notification_state": {
            "day0_sent": day0,
            "sent": [{"event": n[0], "channel": n[1], "at": n[2]} for n in sent],
            "failed": len(failed),
            "last_event": last[0] if last else None,
            "last_at": last[2] if last else None,
        },
        "ops_sync": {"status": row.get("ops_sync_status"), "at": row.get("ops_synced_at")},
    }


# --------------------------------------------------------------------------
# holding period: hold, abandon, reminders
# --------------------------------------------------------------------------

def hold(db, reship_uuid, by, reason):
    """Pause the holding period (administrative / legal / payment dispute).
    A held row is never abandoned; its deadline moves out by the time held
    when the hold is released. Only an open, unpaid row can be held."""
    ensure_schema(db)
    who = (by or "").strip()
    why = (reason or "").strip()
    if not who:
        raise ReshipError("operator_required", "Operator identity required")
    if not why:
        raise ReshipError("reason_required", "A hold needs a reason")
    db.commit()
    row = by_uuid(db, reship_uuid, for_update=True)
    if not row:
        db.rollback()
        raise ReshipError("not_found", "Not found", 404)
    if row["status"] not in PAYABLE:
        db.rollback()
        raise ReshipError("not_holdable", "Only an unpaid, open reship can be held "
                          "(this one is %s)" % row["status"], 409)
    if held(row):
        db.commit()
        return row
    cur = db.cursor()
    cur.execute("UPDATE order_reshipments SET hold_reason=%s, hold_by=%s, hold_at=NOW() "
                "WHERE id=%s AND status IN ('RETURNED','PAYMENT_PENDING') AND hold_reason IS NULL",
                (why[:255], who[:191], row["id"]))
    add_history(cur, row["order_id"], "Reship hold placed by %s: %s" % (who, why[:200]))
    db.commit()
    emit(db, EV_HOLD, row["order_id"], reship_uuid, row.get("customer_id"),
         {"by": who, "reason": why}, suffix=str(uuid.uuid4()))
    return by_uuid(db, reship_uuid)


def release_hold(db, reship_uuid, by, now=None):
    """Lift a hold; the deadline moves out by the time the row was held, so
    the customer keeps the days they were promised."""
    ensure_schema(db)
    who = (by or "").strip()
    if not who:
        raise ReshipError("operator_required", "Operator identity required")
    db.commit()
    row = by_uuid(db, reship_uuid, for_update=True)
    if not row:
        db.rollback()
        raise ReshipError("not_found", "Not found", 404)
    if not held(row):
        db.commit()
        return row
    now = now or db_now(db)
    paused = now - row["hold_at"] if row.get("hold_at") else timedelta(0)
    if paused.total_seconds() < 0:
        paused = timedelta(0)
    cur = db.cursor()
    cur.execute("UPDATE order_reshipments SET hold_reason=NULL, hold_by=NULL, hold_at=NULL, "
                "abandon_at=CASE WHEN abandon_at IS NULL THEN NULL "
                "ELSE abandon_at + INTERVAL %s SECOND END WHERE id=%s",
                (int(paused.total_seconds()), row["id"]))
    add_history(cur, row["order_id"], "Reship hold released by %s after %d day(s); "
                "holding period extended by the same" % (who, paused.days))
    db.commit()
    emit(db, EV_HOLD_RELEASED, row["order_id"], reship_uuid, row.get("customer_id"),
         {"by": who, "paused_seconds": int(paused.total_seconds()),
          "was": row.get("hold_reason")}, suffix=str(uuid.uuid4()))
    return by_uuid(db, reship_uuid)


ABANDONED = "abandoned"
NOT_DUE = "not_due"
HELD = "held"
NOT_OPEN = "not_open"          # paid, reshipped, already abandoned, cancelled
SETTLED = "settled"            # a captured fee was found and applied instead
PAYMENT_UNRESOLVED = "payment_unresolved"


def abandon(db, reship_uuid, now=None, environ=None, fetch_order_payments=None,
            logger=None):
    """Close the holding period on one row, once, if and only if it may be.

    Locks the row and reads it again under the lock: PAID/RESHIPPED (a
    settlement that got there first), a hold, or a deadline not yet reached
    each leave it untouched. A PAYMENT_PENDING row with a Razorpay order is
    asked about at the provider first — a captured payment is settled instead
    of abandoned, and a provider that will not answer postpones the decision.
    Returns ``(outcome, row)``.
    """
    ensure_schema(db)
    days = abandon_days(environ)
    db.commit()
    row = by_uuid(db, reship_uuid, for_update=True)
    if not row:
        db.rollback()
        return NOT_OPEN, None
    if row["status"] not in PAYABLE:
        db.commit()
        return NOT_OPEN, row
    if held(row):
        db.commit()
        return HELD, row
    now = now or db_now(db)
    if not row.get("abandon_at") or row["abandon_at"] > now:
        db.commit()
        return NOT_DUE, row
    if row["status"] == ST_PAYMENT_PENDING and row.get("razorpay_order_id"):
        db.commit()  # release the lock while the provider is asked
        if fetch_order_payments is None:
            return PAYMENT_UNRESOLVED, row
        try:
            payments = fetch_order_payments(row["razorpay_order_id"]) or []
        except Exception as exc:  # noqa: BLE001 - provider down: decide next run
            if logger:
                logger.warning("RESHIP_ABANDON_DEFERRED reship:%s provider unavailable %s"
                               % (reship_uuid, str(exc)[:120]))
            return PAYMENT_UNRESOLVED, row
        captured = [p for p in payments if (p.get("status") or "") == "captured"]
        if captured:
            res = settle_payment(db, reship_uuid, captured[0], "razorpay-reconcile",
                                 logger=logger)
            if res["outcome"] == APPLIED:
                return SETTLED, by_uuid(db, reship_uuid)
            return NOT_OPEN, by_uuid(db, reship_uuid)
        row = by_uuid(db, reship_uuid, for_update=True)
        if row["status"] not in PAYABLE:
            db.commit()
            return NOT_OPEN, row
        if held(row):
            db.commit()
            return HELD, row
        if row["abandon_at"] > now:
            db.commit()
            return NOT_DUE, row
    reason = ABANDON_REASON % days
    cur = db.cursor()
    cur.execute("UPDATE order_reshipments SET status=%s, abandoned_at=NOW(), abandon_reason=%s, "
                "ops_sync_status=%s WHERE id=%s AND status IN ('RETURNED','PAYMENT_PENDING') "
                "AND hold_reason IS NULL",
                (ST_ABANDONED, reason, SYNC_PENDING, row["id"]))
    if cur.rowcount != 1:
        db.rollback()
        return NOT_OPEN, by_uuid(db, reship_uuid)
    add_history(cur, row["order_id"],
                "Returned parcel unclaimed for %d days after receipt (%s) - reship closed as "
                "ABANDONED; goods held subject to policy, no disposal recorded here"
                % (days, row["returned_at"] or row["ops_return_confirmed_at"]))
    db.commit()
    row = by_uuid(db, reship_uuid)
    emit(db, EV_ABANDONED, row["order_id"], reship_uuid, row.get("customer_id"),
         {"reason": reason, "returned_at": row.get("returned_at"),
          "abandon_at": row.get("abandon_at"), "abandoned_at": row.get("abandoned_at")})
    if logger:
        logger.info("ACTIVITY:RESHIP_ABANDONED order:%s reship:%s reason:%s"
                    % (row["order_id"], reship_uuid, reason))
    return ABANDONED, row


def ops_event_payload(row, event=EV_ABANDONED):
    """What Ops is told about an abandonment: identifiers and the reason, no
    customer data."""
    return {"event_id": event_id_for(event, row["reship_uuid"], "ops"),
            "event": event, "order_ref": row["order_id"], "reship_uuid": row["reship_uuid"],
            "abandoned_at": (row.get("abandoned_at").isoformat(sep=" ")
                             if row.get("abandoned_at") else None),
            "reason": row.get("abandon_reason")}


def _default_http_post(url, body, headers, timeout=10):
    req = urlrequest.Request(url, data=body, headers=headers, method="POST")
    with urlrequest.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-configured URL
        return resp.status


def sync_abandoned_to_ops(db, row, environ=None, http_post=None, logger=None):
    """Tell the Ops platform about one abandonment.

    Without ``RESHIP_OPS_WEBHOOK_URL`` the queue API is the channel and the
    row is marked QUEUE at once. With it, the payload is POSTed with an HMAC
    of the body in ``X-Optiwar-Signature``; SENT on 2xx, else FAILED and
    retried by the next sweep. The customer is told nothing here.
    """
    env = os.environ if environ is None else environ
    url = str(env.get(OPS_WEBHOOK_URL_ENV, "")).strip()
    cur = db.cursor()
    if not url:
        cur.execute("UPDATE order_reshipments SET ops_sync_status=%s, ops_synced_at=NOW() "
                    "WHERE id=%s", (SYNC_QUEUE, row["id"]))
        db.commit()
        return SYNC_QUEUE
    payload = ops_event_payload(row)
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    secret = str(env.get(OPS_WEBHOOK_SECRET_ENV, "")).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "X-Optiwar-Event": payload["event"],
               "X-Optiwar-Event-Id": payload["event_id"]}
    if secret:
        headers["X-Optiwar-Signature"] = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    try:
        status = (http_post or _default_http_post)(url, body, headers)
        ok = 200 <= int(status or 0) < 300
        err = None if ok else "http %s" % status
    except Exception as exc:  # noqa: BLE001
        ok, err = False, str(exc)[:160]
    state = SYNC_SENT if ok else SYNC_FAILED
    cur.execute("UPDATE order_reshipments SET ops_sync_status=%s, "
                "ops_synced_at=%s WHERE id=%s",
                (state, datetime.now() if ok else None, row["id"]))
    db.commit()
    if ok:
        emit(db, EV_OPS_SYNC, row["order_id"], row["reship_uuid"], None,
             {"event": payload["event"], "event_id": payload["event_id"]}, suffix="ops")
    else:
        emit(db, EV_OPS_SYNC_FAILED, row["order_id"], row["reship_uuid"], None,
             {"event": payload["event"], "error": err}, suffix=str(uuid.uuid4()))
        if logger:
            logger.warning("RESHIP_OPS_SYNC_FAILED reship:%s %s" % (row["reship_uuid"], err))
    return state


def sweep_holding(db, now=None, host_for=None, mailer=None, whatsapp=None, environ=None,
                  fetch_order_payments=None, http_post=None, logger=None):
    """Reminders on the configured days and abandonment on the deadline, for
    every open unpaid row; plus a retry of any abandonment Ops has not
    acknowledged. ``now`` is injectable for tests; production uses the DB clock.

    Each reminder day is claimed once under ``reship.reminder``/``final_warning``
    keyed by (reship, day); only the latest day reached is sent, so a sweep
    that was down for a fortnight does not deliver three reminders in one go.
    """
    ensure_schema(db)
    summary = {"reminded": 0, "abandoned": 0, "settled": 0, "held": 0, "deferred": 0,
               "synced": 0, "sync_failed": 0}
    if not enabled(environ):
        summary["skipped"] = "disabled"
        return summary
    now = now or db_now(db)
    days_cfg = reminder_days(environ)
    total = abandon_days(environ)
    final = final_window_days(environ)
    cur = db.cursor()
    cur.execute("SELECT reship_uuid FROM order_reshipments WHERE status IN "
                "('RETURNED','PAYMENT_PENDING') AND abandon_at IS NOT NULL ORDER BY id")
    open_rows = [r["reship_uuid"] for r in cur.fetchall()]
    synced_now = set()
    for ruuid in open_rows:
        row = by_uuid(db, ruuid)
        if not row or row["status"] not in PAYABLE:
            continue
        head = _order_head(cur, row["order_id"])
        if head and head.get("is_test"):
            continue
        h = holding(row, now, environ)
        if h["on_hold"]:
            summary["held"] += 1
            continue
        if row["abandon_at"] <= now:
            outcome, row = abandon(db, ruuid, now=now, environ=environ,
                                   fetch_order_payments=fetch_order_payments, logger=logger)
            if outcome == ABANDONED:
                summary["abandoned"] += 1
                host = host_for(row.get("site_from")) if host_for else row.get("site_from")
                notify(db, EV_ABANDONED, row["order_id"], row.get("customer_id"), host,
                       reship_uuid=ruuid, mailer=mailer, whatsapp=whatsapp, environ=environ,
                       fields=holding_fields(row, now, environ))
                state = sync_abandoned_to_ops(db, row, environ, http_post, logger)
                summary["synced" if state != SYNC_FAILED else "sync_failed"] += 1
                synced_now.add(ruuid)
            elif outcome == SETTLED:
                summary["settled"] += 1
                host = host_for(row.get("site_from")) if host_for else row.get("site_from")
                notify(db, EV_PAYMENT_COMPLETED, row["order_id"], row.get("customer_id"), host,
                       reship_uuid=ruuid, mailer=mailer, whatsapp=whatsapp, environ=environ)
            elif outcome == PAYMENT_UNRESOLVED:
                summary["deferred"] += 1
            elif outcome == HELD:
                summary["held"] += 1
            continue
        reached = [d for d in days_cfg if h["days_held"] is not None and h["days_held"] >= d]
        if not reached:
            continue
        latest = reached[-1]
        for d in reached:
            final_one = (total - d) <= final
            ev = EV_FINAL_WARNING if final_one else EV_REMINDER
            if not emit(db, ev, row["order_id"], ruuid, row.get("customer_id"),
                        {"day": d, "days_remaining": total - d, "sent": d == latest},
                        suffix="day%d" % d):
                continue
            if d != latest:
                continue
            host = host_for(row.get("site_from")) if host_for else row.get("site_from")
            fields = holding_fields(row, now, environ)
            res = notify(db, ev, row["order_id"], row.get("customer_id"), host,
                         reship_uuid=ruuid, mailer=mailer, whatsapp=whatsapp, environ=environ,
                         fields=fields, suffix="%s:day%d" % (ev, d))
            if res.get("sent"):
                summary["reminded"] += 1
    cur.execute("SELECT reship_uuid FROM order_reshipments WHERE status='ABANDONED' "
                "AND (ops_sync_status IS NULL OR ops_sync_status IN ('PENDING','FAILED'))")
    for r in cur.fetchall():
        if r["reship_uuid"] in synced_now:
            continue
        row = by_uuid(db, r["reship_uuid"])
        state = sync_abandoned_to_ops(db, row, environ, http_post, logger)
        summary["synced" if state != SYNC_FAILED else "sync_failed"] += 1
    return summary


def holding_fields(row, now=None, environ=None):
    """Template fields for the holding-period messages, from the row alone."""
    h = holding(row, now, environ)
    fmt = lambda d: d.strftime("%d %b %Y") if d else "n/a"  # noqa: E731
    return {"deadline": fmt(h["abandon_at"]), "returned_on": fmt(h["returned_at"]),
            "days_remaining": h["days_remaining"] if h["days_remaining"] is not None else "",
            "holding_days": h["holding_days"], "support": SUPPORT_EMAIL}


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
    # The three holding-period templates below are gated separately
    # (RESHIP_WA_REMINDER_TEMPLATES_APPROVED) until Meta approves them.
    "reship_reminder": {
        "header": "Optiwar — Your Returned Package Is Waiting",
        "body": ("Your returned package for order {{1}} is being held at Optiwar for you.\n\n"
                 "Reship it by paying the ₹250 reshipping charge before {{2}} "
                 "({{3}} days remaining). Open My Orders:\n{{4}}\n\n"
                 "After that date the package is treated as abandoned and reshipment is "
                 "no longer available online."),
        "variables": {"1": "order id", "2": "deadline", "3": "days remaining",
                      "4": "My Orders URL"},
    },
    "reship_final_warning": {
        "header": "Optiwar — Final Reshipment Period",
        "body": ("Final notice for order {{1}}: your returned package will be treated as "
                 "abandoned on {{2}} ({{3}} days remaining).\n\n"
                 "To have it reshipped, pay the ₹250 reshipping charge in My Orders before "
                 "then:\n{{4}}"),
        "variables": {"1": "order id", "2": "deadline", "3": "days remaining",
                      "4": "My Orders URL"},
    },
    "reship_abandoned": {
        "header": "Optiwar — Holding Period Ended",
        "body": ("The {{2}}-day holding period for the returned package of order {{1}} has "
                 "ended and the package is now treated as abandoned. Reshipment is no "
                 "longer available online.\n\n"
                 "If you believe this is a mistake, write to {{3}}."),
        "variables": {"1": "order id", "2": "holding days", "3": "support email"},
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
        "Open My Orders to continue:\n{url}\n\nWe hold the package for you for "
        "{holding_days} days from the day it reached us ({returned_on}), i.e. until "
        "{deadline}. If the reshipping charge is not paid by then, the package is "
        "treated as abandoned and reshipment is no longer available online.\n\n"
        "Before paying, please check that the "
        "delivery address and phone number on the order are correct. If anything needs "
        "to change, reply to this email or write to support@optiwar.com and we will "
        "update it before dispatch.\n\nPrescription/customized lenses are not "
        "eligible for cancellation after preparation.\n\nSafety notice: make payments "
        "only through Optiwar's official website. Optiwar will never ask you to share "
        "OTPs, passwords, card details or banking credentials.\n\nRegards,\n"
        "Optiwar Support\n"),
    EV_PAYMENT_COMPLETED: (
        "Optiwar — reshipping confirmed for order {order_id}",
        "Hello {name},\n\nWe have received your Rs 250 reshipping charge for order "
        "{order_id}.\n\nYour package will now be prepared for reshipment. We will send "
        "the new tracking details after dispatch.\n\nRegards,\nOptiwar Support\n"),
    EV_REMINDER: (
        "Optiwar — your returned package is waiting (order {order_id})",
        "Hello {name},\n\nYour returned package for order {order_id} is being held at "
        "Optiwar for you.\n\nYou can have it reshipped by paying the Rs 250 reshipping "
        "charge in My Orders before {deadline} ({days_remaining} days remaining):\n{url}"
        "\n\nAfter that date the package is treated as abandoned and reshipment is no "
        "longer available online.\n\nIf the delivery address or phone number needs to "
        "change, reply to this email or write to {support} before paying.\n\n"
        "Safety notice: make payments only through Optiwar's official website. Optiwar "
        "will never ask you to share OTPs, passwords, card details or banking "
        "credentials.\n\nRegards,\nOptiwar Support\n"),
    EV_FINAL_WARNING: (
        "Optiwar — final reshipment period for order {order_id}",
        "Hello {name},\n\nThis is the final notice for order {order_id}: your returned "
        "package will be treated as abandoned on {deadline} ({days_remaining} days "
        "remaining).\n\nTo have it reshipped, pay the Rs 250 reshipping charge in My "
        "Orders before then:\n{url}\n\nIf the delivery address or phone number needs to "
        "change, reply to this email or write to {support} before paying.\n\n"
        "Safety notice: make payments only through Optiwar's official website. Optiwar "
        "will never ask you to share OTPs, passwords, card details or banking "
        "credentials.\n\nRegards,\nOptiwar Support\n"),
    EV_ABANDONED: (
        "Optiwar — holding period ended for order {order_id}",
        "Hello {name},\n\nThe {holding_days}-day holding period for the returned package "
        "of order {order_id} ended on {deadline}. The package is now treated as abandoned "
        "under Optiwar's Shipping Terms and reshipment is no longer available online.\n\n"
        "If you believe this is a mistake, write to {support} quoting the order id.\n\n"
        "Regards,\nOptiwar Support\n"),
}
_WA_FOR_EVENT = {EV_RETURN_STARTED: "return_started", EV_AVAILABLE: "reship_available",
                 EV_PAYMENT_COMPLETED: "reship_paid", EV_REMINDER: "reship_reminder",
                 EV_FINAL_WARNING: "reship_final_warning", EV_ABANDONED: "reship_abandoned"}
_WA_HOLDING_EVENTS = (EV_REMINDER, EV_FINAL_WARNING, EV_ABANDONED)


def _wa_components(event_type, fields):
    order_id = fields["order_id"]
    if event_type in (EV_REMINDER, EV_FINAL_WARNING):
        return {"body_1": {"type": "text", "value": order_id},
                "body_2": {"type": "text", "value": str(fields.get("deadline", ""))},
                "body_3": {"type": "text", "value": str(fields.get("days_remaining", ""))},
                "body_4": {"type": "text", "value": fields["url"]}}
    if event_type == EV_ABANDONED:
        return {"body_1": {"type": "text", "value": order_id},
                "body_2": {"type": "text", "value": str(fields.get("holding_days", ""))},
                "body_3": {"type": "text", "value": SUPPORT_EMAIL}}
    comps = {"body_1": {"type": "text", "value": order_id}}
    if event_type == EV_AVAILABLE:
        comps["body_2"] = {"type": "text", "value": fields["url"]}
    return comps


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
           mailer=None, whatsapp=None, environ=None, key=None, fields=None, suffix=None,
           now=None):
    """Send the customer the message for one logical event, once per channel.

    The claim is the ``reship.notified``/``notify_failed`` event id derived
    from (event, key, channel): a second call for the same event finds it and
    sends nothing. A failed delivery is recorded and never changes the reship.
    ``suffix`` distinguishes repeats of one event type (reminder day 30 vs 45);
    the holding-period fields (deadline, days left) come from the reship row.
    """
    env = os.environ if environ is None else environ
    if event_type not in EMAILS:
        return {"sent": False, "reason": "no_template"}
    cur = db.cursor()
    acct = _account(cur, customer_id)
    name = (acct.get("customer_name") or "").strip() or "Customer"
    row = by_uuid(db, reship_uuid) if reship_uuid else None
    merged = holding_fields(row, now, environ) if row else {
        "deadline": "n/a", "returned_on": "n/a", "days_remaining": "",
        "holding_days": abandon_days(environ), "support": SUPPORT_EMAIL}
    merged.update({"name": name, "order_id": str(order_id), "url": my_orders_url(host)})
    merged.update(fields or {})
    fields = merged
    tag = suffix or event_type
    claim_key = key or reship_uuid or str(order_id)
    out = {"sent": False, "email": None, "whatsapp": None}

    email = (acct.get("customer_email") or "").strip()
    if email and "@" in email:
        claimed = emit(db, EV_NOTIFIED, order_id, reship_uuid, customer_id,
                       {"event": event_type, "channel": "email", "tag": tag},
                       key=claim_key, suffix="%s:email" % tag)
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
                     key=claim_key, suffix="%s:email:fail" % tag)

    phone = (acct.get("customer_phone") or "").strip()
    gate = WA_REMINDERS_APPROVED_ENV if event_type in _WA_HOLDING_EVENTS else WA_APPROVED_ENV
    approved = str(env.get(gate, "")).strip().lower() in ("1", "true", "yes")
    if phone and approved:
        claimed = emit(db, EV_NOTIFIED, order_id, reship_uuid, customer_id,
                       {"event": event_type, "channel": "whatsapp", "tag": tag},
                       key=claim_key, suffix="%s:whatsapp" % tag)
        if claimed:
            tpl = env.get(WA_TEMPLATE_PREFIX_ENV, "") + _WA_FOR_EVENT[event_type]
            comps = _wa_components(event_type, fields)
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
                     key=claim_key, suffix="%s:whatsapp:fail" % tag)
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


RESHIP_CC = "admin@optiwar.com"


def _default_mailer(to_email, subject, text):
    from flask import current_app
    from flask_mail import Message
    cc = [RESHIP_CC] if RESHIP_CC.lower() != (to_email or "").lower() else []
    msg = Message(subject=subject, recipients=[to_email], cc=cc, body=text,
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
