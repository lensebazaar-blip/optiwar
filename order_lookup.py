#!/usr/bin/env python3
"""LOOKUP_ORDER: the orders and payments a signed-in customer may be told.

Read-only, and scoped to the customer signed in on this browser (the Flask
session's login, never an id the chat widget sent) and to the site the chat is
on. An order is "paid" exactly when ``customer_orders``/My Orders calls it
paid; an attempt the gateway never confirmed is said to be unpaid, never
guessed into a success. Stages are the ones My Orders shows.
"""
from collections import OrderedDict

try:
    from .customer_orders import ORDER_LINES_SQL, stage
    from .paid_orders import payment_state
except ImportError:  # loaded standalone by the tests
    from customer_orders import ORDER_LINES_SQL, stage
    from paid_orders import payment_state

ORDER_LOOKBACK_DAYS = 180
UNPAID_LOOKBACK_DAYS = 14
MAX_ORDERS = 4
MAX_UNPAID = 2
MAX_LINES = 80
TRACKABLE = ("Shipped", "Delivery-assist")


def _date(v):
    return v.strftime("%d %b %Y") if hasattr(v, "strftime") else str(v or "")[:10]


def _age_days(v, now):
    if not hasattr(v, "date") or now is None:
        return 0
    return (now - v).days


def group(rows, now=None, include_test=False):
    """``{"orders": [...], "unpaid": [...]}`` from ``ORDER_LINES_SQL`` rows,
    newest first. Test orders are nobody's order, except a TEST customer's."""
    grouped = OrderedDict()
    for r in rows or []:
        if r.get("is_test_order") and not include_test:
            continue
        o = grouped.get(r["order_id"])
        if o is None:
            status = r.get("order_status_name")
            o = grouped[r["order_id"]] = {
                "order_id": str(r["order_id"]),
                "created": r.get("date_created"),
                "date": _date(r.get("date_created")),
                "status_name": status or "",
                "payment": payment_state(r.get("payment_date") is not None, status),
                "stage": stage(status)[0],
                "items": [],
            }
        name = str(r.get("product_name") or "")[:60]
        if name and name not in o["items"]:
            o["items"].append(name)
    model = {"orders": [], "unpaid": []}
    for o in grouped.values():
        if o["payment"] == "paid":
            if len(model["orders"]) < MAX_ORDERS:
                model["orders"].append(o)
        elif (len(model["unpaid"]) < MAX_UNPAID
              and _age_days(o["created"], now) <= UNPAID_LOOKBACK_DAYS):
            model["unpaid"].append(o)
    for o in model["orders"] + model["unpaid"]:
        o.pop("created", None)
    return model


def read_model(db, customer_id, site_from=None, shipments=None, now=None,
               include_test=False):
    """The signed-in customer's recent orders on this site; ``shipments`` is
    ``reship.shipments_for_orders`` (passed in so this module stays pure)."""
    cur = db.cursor()
    sql = (ORDER_LINES_SQL + "WHERE o.customer_id = %s AND o.date_created >= NOW() - "
           "INTERVAL " + str(int(ORDER_LOOKBACK_DAYS)) + " DAY ")
    params = [customer_id]
    if site_from:
        sql += "AND o.site_from = %s "
        params.append(site_from)
    sql += "ORDER BY o.date_created DESC LIMIT %d" % MAX_LINES
    cur.execute(sql, tuple(params))
    model = group(cur.fetchall(), now=now, include_test=include_test)
    track = [o["order_id"] for o in model["orders"] if o["status_name"] in TRACKABLE]
    found = shipments(db, track) if (shipments and track) else {}
    for o in model["orders"]:
        awb, courier = found.get(o["order_id"], ("", ""))
        o["awb"], o["courier"] = awb, courier
    return model


def found(model):
    return bool(model and (model.get("orders") or model.get("unpaid")))


def prompt_section(model):
    """The block the model answers an order or payment question from."""
    lines = ["", "ORDERS ON FILE (LOOKUP_ORDER: read just now from this signed-in "
             "customer's own account on this site; authoritative, same as My Orders):"]
    for o in (model or {}).get("orders") or []:
        track = ""
        if o.get("awb"):
            track = "; tracking AWB %s%s" % (o["awb"], (" (%s)" % o["courier"])
                                              if o.get("courier") else "")
        lines.append("  Order %s placed %s: payment received; stage %s; items: %s%s"
                     % (o["order_id"], o["date"], o["stage"],
                        ", ".join(o["items"]) or "-", track))
    for o in (model or {}).get("unpaid") or []:
        lines.append("  Checkout %s started %s: payment %s — this is NOT an order being "
                     "made; items: %s"
                     % (o["order_id"], o["date"],
                        "failed" if o["payment"] == "failed" else "not received",
                        ", ".join(o["items"]) or "-"))
    if not found(model):
        lines.append("  Nothing on file: no paid order and no recent checkout on this site "
                     "for this account.")
    lines.append("  Quote order ids, stages and AWBs exactly; never invent a date, AWB or "
                 "delivery estimate. If money left the customer's account for a checkout "
                 "listed as not received, say the payment has not reached Optiwar yet and "
                 "offer a support ticket so the payment can be traced. Full details and "
                 "tracking are in My Orders (/profile/?tab=orders).")
    return "\n".join(lines) + "\n"


def event_payload(model):
    """Counts only — no order id, no item, no AWB."""
    m = model or {}
    return {"paid_orders": len(m.get("orders") or []),
            "unpaid_checkouts": len(m.get("unpaid") or []),
            "with_tracking": sum(1 for o in m.get("orders") or [] if o.get("awb")),
            "found": found(m)}


SIGN_IN_ORDERS_URL = "/auth/login?next=%2Fprofile%2F%3Ftab%3Dorders"
MY_ORDERS_URL = "/profile/?tab=orders"

SIGNED_OUT_SECTION = (
    "\nACCOUNT: the customer is NOT signed in on this browser. Their orders, payments, "
    "saved prescriptions and returned parcels are only shown after they sign in. For a "
    "question about any of those, do not ask for an order number or email and do not "
    "offer a support ticket: tell them in their language to sign in and open My Orders "
    "(the server adds the sign-in button under your reply). General questions "
    "(products, prices, shipping, returns policy, prescriptions in general) are "
    "answered without signing in.\n")
