"""Delhivery reverse pickup for Optiwar orders.

Ops books a Delhivery 'Pickup' (reverse) shipment from the customer's address
back to our registered warehouse. The waybill lives in our own table
(ow_reverse_pickups) - never in ops_shipping_awb, which Optiwar reads as the
forward-shipment history. Customer email/WhatsApp is Optiwar's: after booking
we tell Optiwar through its Ops API and it notifies the customer. A failed call
leaves a retryable sync row (same mechanism as the reship syncs)."""
import json
import logging

from database import db_cursor
from services import delhivery
from services import reship

log = logging.getLogger(__name__)

SYNC_KIND = "reverse_pickup"
SYNC_CANCEL_KIND = "reverse_pickup_cancel"
PATH = "/ops/api/shipments/%s/reverse-pickup"
CANCEL_PATH = "/ops/api/shipments/%s/reverse-pickup/cancel"
REASONS = ("MANUFACTURING DEFECT", "Wrong power / Rx issue", "Damaged in transit", "Wrong product sent",
           "Frame fit / size issue", "Customer return (discretionary)", "Other")


def init_tables():
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ow_reverse_pickups (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ow_order_id VARCHAR(40) NOT NULL,
                reference VARCHAR(60) NOT NULL,
                courier VARCHAR(30) NOT NULL DEFAULT 'Delhivery',
                waybill VARCHAR(40) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'booked',
                reason VARCHAR(120) DEFAULT '',
                remarks VARCHAR(500) DEFAULT '',
                pickup_name VARCHAR(120) DEFAULT '',
                pickup_address VARCHAR(500) DEFAULT '',
                pickup_pin VARCHAR(12) DEFAULT '',
                pickup_phone VARCHAR(30) DEFAULT '',
                forward_awb VARCHAR(40) DEFAULT '',
                weight_kg DECIMAL(6,3) DEFAULT 0.25,
                courier_response LONGTEXT,
                created_by INT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                cancelled_at DATETIME NULL,
                UNIQUE KEY (waybill), INDEX (ow_order_id)
            )""")


def list_for(order_id):
    with db_cursor() as cur:
        cur.execute("""SELECT p.*, u.full_name AS created_by_name FROM ow_reverse_pickups p
                       LEFT JOIN platform_users u ON u.id = p.created_by
                       WHERE p.ow_order_id = %s ORDER BY p.id DESC""", (order_id,))
        return cur.fetchall()


def get(pickup_id):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM ow_reverse_pickups WHERE id = %s", (pickup_id,))
        return cur.fetchone()


def active_for(order_id):
    return [p for p in list_for(order_id) if p["status"] != "cancelled"]


def next_reference(order_id):
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM ow_reverse_pickups WHERE ow_order_id = %s",
                    (order_id,))
        n = (cur.fetchone() or {}).get("n") or 0
    return "OW-%s-RP%d" % (order_id, n + 1)


def _mobile(raw):
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def serviceable(pin):
    """Delhivery pin record, with pickup=Y meaning reverse pickup is possible."""
    try:
        pc = delhivery.check_serviceability(pin)
    except delhivery.DelhiveryError as e:
        return {"error": str(e)}
    return pc or {}


def book(order, params, user, forward_awb=""):
    """Book the reverse pickup with Delhivery, store it, then tell Optiwar.
    Returns (pickup_row, sync_row)."""
    order_id = order["order_id"]
    if active_for(order_id):
        raise delhivery.DelhiveryError("A reverse pickup is already booked for this order - "
                                       "cancel it first if a new one is needed")
    ref = next_reference(order_id)
    pin = str(order.get("shipping_postcode") or "").strip()
    sc = serviceable(pin)
    if sc.get("pickup") != "Y":
        raise delhivery.DelhiveryError("Delhivery does not offer reverse pickup from %s" % pin)
    wh = delhivery._pickup_location()
    p = dict(params, payment_mode="Pickup", reference=ref,
             city=params.get("city") or order.get("shipping_city") or sc.get("city")
             or sc.get("district") or "",
             extra_fields={"phone": _mobile(order.get("telephone")),
                           "return_name": wh["name"], "return_add": wh["add"],
                           "return_city": wh["city"], "return_pin": wh["pin_code"],
                           "return_country": wh["country"], "return_phone": wh["phone"]})
    resp = delhivery.create_waybill(order, p)
    wb = delhivery.extract_waybill(resp)
    if not wb:
        raise delhivery.DelhiveryError("Delhivery returned no waybill",
                                       {"response": json.dumps(resp)[:2000]})
    sh = delhivery.build_shipment(order, p)
    with db_cursor() as cur:
        cur.execute("""INSERT INTO ow_reverse_pickups (ow_order_id, reference, waybill, reason,
                       remarks, pickup_name, pickup_address, pickup_pin, pickup_phone,
                       forward_awb, weight_kg, courier_response, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (order_id, ref, wb, (params.get("reason") or "")[:120],
                     (params.get("remarks") or "")[:500], sh["name"][:120], sh["add"][:500],
                     sh["pin"], sh["phone"], forward_awb or "", params.get("weight_kg") or 0.25,
                     json.dumps(resp), (user or {}).get("id")))
        pid = cur.lastrowid
    payload = {"operator": reship._operator(user), "courier": "Delhivery", "awb": wb,
               "reference": ref, "reason": params.get("reason") or "",
               "remarks": params.get("remarks") or "", "forward_awb": forward_awb or "",
               "track_url": "https://www.delhivery.com/track/package/%s" % wb,
               "pickup": {"name": sh["name"], "address": sh["add"], "city": sh["city"],
                          "pin": sh["pin"], "phone": sh["phone"]}}
    sid = reship._new_sync(order_id, SYNC_KIND, payload, (user or {}).get("id"),
                           local_ref_id=pid)
    return get(pid), reship.run_sync(sid)


def cancel(pickup_id, user):
    row = get(pickup_id)
    if not row or row["status"] == "cancelled":
        return row, None
    delhivery.cancel_waybill(row["waybill"])
    with db_cursor() as cur:
        cur.execute("UPDATE ow_reverse_pickups SET status = 'cancelled', cancelled_at = NOW() "
                    "WHERE id = %s", (pickup_id,))
    sync = None
    with db_cursor() as cur:
        cur.execute("SELECT id FROM ow_reship_sync WHERE kind = %s AND local_ref_id = %s "
                    "AND state = 'synced' LIMIT 1", (SYNC_KIND, pickup_id))
        told = cur.fetchone()
        if not told:
            # Optiwar never heard of it: drop the pending announcement instead
            # of later telling the customer about a cancelled pickup.
            cur.execute("UPDATE ow_reship_sync SET state = 'synced', last_error = %s "
                        "WHERE kind = %s AND local_ref_id = %s AND state <> 'synced'",
                        ("superseded: pickup cancelled before Optiwar sync", SYNC_KIND,
                         pickup_id))
    if told:
        sid = reship._new_sync(row["ow_order_id"], SYNC_CANCEL_KIND,
                               {"operator": reship._operator(user), "awb": row["waybill"]},
                               (user or {}).get("id"), local_ref_id=pickup_id)
        sync = reship.run_sync(sid)
    return get(pickup_id), sync


def notification_view(syncs):
    """Per-channel customer notification state Optiwar reported for the latest
    synced call (booking or cancellation): SENT / FAILED / SKIPPED (reason)."""
    done = [x for x in syncs if x["state"] == "synced" and x.get("response")]
    if not done:
        return None
    latest = max(done, key=lambda x: x["id"])
    try:
        data = json.loads(latest["response"])
    except (TypeError, ValueError):
        return None
    rp = data.get("reverse_pickup") or {}
    ns = rp.get("notification_state") or {}
    chans = {}
    for item in ns.get("sent") or []:
        if isinstance(item, dict):
            chans[str(item.get("channel") or "?")] = ("SENT", item.get("at") or "")
    for item in ns.get("skipped") or []:
        if isinstance(item, dict):
            chans.setdefault(str(item.get("channel") or "?"),
                             ("SKIPPED", item.get("reason") or ""))
    for item in (ns.get("failed_detail") or ns.get("failures") or []):
        if isinstance(item, dict):
            chans[str(item.get("channel") or "?")] = ("FAILED", item.get("reason") or "")
    return {"kind": latest["kind"], "status": rp.get("status") or "",
            "optiwar_id": rp.get("id") or "", "channels": chans,
            "failed": ns.get("failed") or 0, "raw": ns}
