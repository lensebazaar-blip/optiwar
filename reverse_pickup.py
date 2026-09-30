"""India reverse pickup: Ops books a Delhivery reverse waybill, Optiwar
records it against the order and tells the customer.

Ops owns the courier booking; this module owns the customer's side of it:
one ``order_reverse_pickups`` row per waybill, an order-history line, the
email / WhatsApp notice (once per channel, claimed in ``reship_events``),
and the My Orders card. The forward AWB is never rewritten, the customer
tracking link is built here from the AWB (never taken from the request), and
a replay of the same AWB returns the stored row without a second message.
"""
import json
import os
import uuid

try:
    from . import reship
    from .paid_orders import add_history
except ImportError:  # pragma: no cover - flat import in scripts
    import reship
    from paid_orders import add_history

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

TABLES = [("order_reverse_pickups", TABLE_DDL)]

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
    db.commit()
    return by_awb(db, awb), True


def cancel(db, order_id, body, operator):
    """Cancel an accepted booking. Returns ``(row, changed)``; cancelling an
    already cancelled AWB returns it unchanged."""
    ensure_schema(db)
    oid = resolve_order_id(db, order_id)
    if not oid:
        raise ReversePickupError("not_found", "order not found", 404)
    awb = _normal_awb(body.get("awb"))
    row = by_awb(db, awb, for_update=True) if awb else active_for_order(db, oid, for_update=True)
    if not row or row["order_id"] != oid:
        db.rollback()
        raise ReversePickupError("not_found", "no reverse pickup with this AWB on this order", 404)
    if row["status"] == ST_CANCELLED:
        db.rollback()
        return row, False
    cur = db.cursor()
    cur.execute("UPDATE order_reverse_pickups SET status=%s, cancelled_by=%s, cancelled_at=NOW() "
                "WHERE id=%s AND status=%s", (ST_CANCELLED, _clip(operator, 191), row["id"], ST_BOOKED))
    add_history(cur, oid, "Reverse pickup cancelled with Delhivery, AWB %s, by %s"
                % (row["awb"], _clip(operator, 120)), row.get("site_from"))
    reship.emit(db, EV_CANCELLED, oid, row["pickup_uuid"], row.get("customer_id"),
                {"awb": row["awb"], "operator": _clip(operator, 120)}, key=row["pickup_uuid"], commit=False)
    db.commit()
    return by_awb(db, row["awb"]), True


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
              "terms_url": _host_url(host, "/terms-and-conditions"),
              "url": reship.my_orders_url(host)}
    key = row["pickup_uuid"]

    def claim(channel):
        return reship.emit(db, EV_NOTIFIED, row["order_id"], key, row.get("customer_id"),
                           {"event": event_type, "channel": channel}, key=key,
                           suffix="%s:%s" % (event_type, channel))

    def failed(channel, exc):
        reship.emit(db, EV_NOTIFY_FAILED, row["order_id"], key, row.get("customer_id"),
                    {"event": event_type, "channel": channel, "error": str(exc)[:160]},
                    key=key, suffix="%s:%s:fail" % (event_type, channel))

    email = (acct.get("customer_email") or "").strip()
    if email and "@" in email and claim("email"):
        subject, text = EMAILS[event_type]
        try:
            (mailer or reship._default_mailer)(email, subject.format(**fields), text.format(**fields))
        except Exception as exc:  # noqa: BLE001
            failed("email", exc)

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
