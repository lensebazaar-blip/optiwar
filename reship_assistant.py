"""What the assistant may know and say about a returned India parcel.

Every fact here is read from the reship ledger through ``reship.public_view``
— the same projection the customer's My Orders card draws — so the assistant
and the page never disagree. The model receives the facts as a prompt
section and a set of rules; it receives no write path. The only action it
may offer is navigation to My Orders (``[ACTION:NAVIGATE:...]``), which goes
through the ACR offer -> confirm -> execute lifecycle like any other.

Canonical states (``public_view['state']`` plus the two the deadline adds):

    RETURNING_TO_OPS         courier says Returned; Optiwar does not have it yet
    RETURNED_TO_OPS          Ops physically has it; INR 250 is payable
      RESHIP_AVAILABLE / AWAITING_PAYMENT   sub-states of RETURNED_TO_OPS
      ABANDONMENT_APPROACHING               RETURNED_TO_OPS in the final window
    RESHIP_PAID              fee captured; Ops prepares the shipment
    RESHIPPED                sent again on a new AWB
    ABANDONED                held the whole period, never paid
"""
import re
from datetime import datetime

try:
    from . import reship
except ImportError:  # pragma: no cover - flat test import
    import reship

MY_ORDERS_PATH = "/profile/?tab=orders"
MAX_ORDERS = 30

STATE_RETURNING = reship.LOG_RETURNING
STATE_RETURNED = reship.LOG_RETURNED
STATE_PAID = reship.LOG_RESHIP_PAID
STATE_RESHIPPED = reship.LOG_RESHIPPED
STATE_ABANDONED = reship.LOG_ABANDONED
SUB_AVAILABLE = "RESHIP_AVAILABLE"
SUB_AWAITING = "AWAITING_PAYMENT"
SUB_APPROACHING = "ABANDONMENT_APPROACHING"
SUB_HELD = "ON_HOLD"

RULES = """RETURNED-PARCEL RULES (India only; authoritative, from the reship ledger):
- If the courier cannot deliver and brings the parcel back, the order is NOT cancelled and the
  amount paid is NOT refunded for that reason. Custom-made prescription glasses cannot be
  cancelled because delivery failed.
- The customer can have the parcel shipped again for a fixed INR 250 reshipping charge, paid in
  My Orders (%(path)s) on its own payment. Never quote any other fee and never offer to change it.
- The INR 250 can be paid ONLY when the state below is RETURNED_TO_OPS (Optiwar physically has
  the parcel; can_pay=yes). A courier status of "Returned" / RTO / RETURNING_TO_OPS means the
  parcel is still travelling back: tell the customer we will notify them when it arrives, and do
  NOT say they can pay yet.
- A returned parcel is held %(days)s days counted from the day our team physically receives it
  (not from the courier's RTO date). The deadline below (abandon_at) is the truth; do not compute
  your own. After it the parcel is treated as abandoned and cannot be reshipped online.
- A parcel whose INR 250 is PAID (RESHIP_PAID / RESHIPPED) is never abandoned. If the state is
  RESHIP_PAID, the payment is received: never ask the customer to pay again; Ops is preparing it.
- Give a new AWB / tracking link only when the state is RESHIPPED and new_awb is present below.
  Never invent an AWB or a date.
- If the state is ABANDONED, say so plainly and direct the customer to %(support)s; do not
  promise reinstatement.
- If the state is ON_HOLD, payment is paused while our team reviews it; do not offer payment.
- You cannot mark a parcel received, paid, shipped or abandoned, and cannot extend a deadline:
  those are done by Ops/the payment provider. For anything beyond these facts offer a support
  ticket (ask first, per the ticket rule).
- To take the customer to My Orders end with [ACTION:NAVIGATE:%(path)s] after offering.
- Answer only from the facts below. If an order is not listed, say you have no returned-parcel
  record for it and offer to check with support."""


def enabled(host, environ=None):
    """The section exists only where the workflow does: flag on, India host."""
    return reship.enabled(environ) and reship.is_india_host(host)


def _fmt(d):
    if isinstance(d, datetime):
        return d.strftime("%d %b %Y")
    return str(d) if d else None


def _sub_state(view):
    if view["state"] != STATE_RETURNED:
        return None
    if view["on_hold"]:
        return SUB_HELD
    if view["final_period"]:
        return SUB_APPROACHING
    return SUB_AWAITING if view.get("payment_pending") else SUB_AVAILABLE


def _customer_order_ids(cur, customer_id):
    cur.execute("SELECT order_id, MIN(site_from) AS site_from, MAX(date_created) AS d "
                "FROM orders WHERE customer_id=%s AND (is_test IS NULL OR is_test=0) "
                "GROUP BY order_id ORDER BY d DESC LIMIT %s",
                (int(customer_id), MAX_ORDERS))
    return [(r["order_id"], r.get("site_from")) for r in cur.fetchall()]


def read_model(db, customer_id, host, environ=None, now=None):
    """Every returned-parcel fact this customer may be told: one entry per
    order in the workflow, or none. Read-only; no provider ids."""
    out = {"orders": [], "fee": reship.FEE_INR, "currency": reship.CURRENCY,
           "holding_days": reship.abandon_days(environ), "support": reship.SUPPORT_EMAIL}
    if not customer_id or not enabled(host, environ):
        return out
    cur = db.cursor()
    rows = reship.for_customer(db, customer_id)
    now = now or reship.db_now(db)
    for order_id, site in _customer_order_ids(cur, customer_id):
        if not reship.workflow_open(host, site, order_id, environ):
            continue
        row = rows.get(order_id)
        latest = reship._latest_status(cur, order_id)
        view = reship.public_view(row, latest, shipment=reship.original_shipment(cur, order_id),
                                  now=now, environ=environ)
        if view is None:
            continue
        view["payment_pending"] = bool(row and row["status"] == reship.ST_PAYMENT_PENDING)
        out["orders"].append(_entry(order_id, view))
    return out


def _entry(order_id, view):
    return {
        "order_id": order_id,
        "state": view["state"],
        "sub_state": _sub_state(view),
        "can_pay": bool(view["can_pay"]),
        "fee": view["fee"],
        "currency": view["currency"],
        "paid_at": view["paid_at"],
        "returned_at": view["returned_at"],
        "abandon_at": view["abandon_at"],
        "days_remaining": view["days_remaining"],
        "holding_days": view["holding_days"],
        "final_period": bool(view["final_period"]),
        "on_hold": bool(view["on_hold"]),
        "abandoned_at": view["abandoned_at"],
        "reshipped_at": view["reshipped_at"],
        "original_awb": view["original_awb"],
        "original_courier": view["original_courier"],
        "original_track_url": view["original_track_url"],
        "new_awb": view["new_awb"],
        "new_courier": view["new_courier"],
        "new_track_url": view["new_track_url"],
        "reship_uuid": view["reship_uuid"],
    }


def _find(model, order_id):
    oid = str(order_id or "").strip().upper()
    for e in model["orders"]:
        if str(e["order_id"]).upper() == oid:
            return e
    return None


# ---------------------------------------------------------------- read tools
# Each answers one customer question from the model alone; ``None`` means
# "no returned-parcel record for that order" and must be said as such.

def get_order_reship_status(model, order_id):
    e = _find(model, order_id)
    if not e:
        return None
    return {"order_id": e["order_id"], "state": e["state"], "sub_state": e["sub_state"],
            "can_pay": e["can_pay"], "on_hold": e["on_hold"]}


def get_return_status(model, order_id):
    e = _find(model, order_id)
    if not e:
        return None
    return {"order_id": e["order_id"],
            "physically_received": e["state"] != STATE_RETURNING,
            "returned_at": e["returned_at"], "original_awb": e["original_awb"],
            "original_courier": e["original_courier"],
            "original_track_url": e["original_track_url"]}


def get_reship_payment_status(model, order_id):
    e = _find(model, order_id)
    if not e:
        return None
    paid = e["state"] in (STATE_PAID, STATE_RESHIPPED)
    return {"order_id": e["order_id"], "fee": e["fee"], "currency": e["currency"],
            "paid": paid, "paid_at": e["paid_at"], "can_pay": e["can_pay"],
            "pay_at": MY_ORDERS_PATH if e["can_pay"] else None}


def get_reship_tracking(model, order_id):
    e = _find(model, order_id)
    if not e:
        return None
    shipped = e["state"] == STATE_RESHIPPED and bool(e["new_awb"])
    return {"order_id": e["order_id"], "reshipped": shipped,
            "new_awb": e["new_awb"] if shipped else None,
            "new_courier": e["new_courier"] if shipped else None,
            "new_track_url": e["new_track_url"] if shipped else None,
            "reshipped_at": e["reshipped_at"] if shipped else None}


def get_reship_holding_deadline(model, order_id):
    e = _find(model, order_id)
    if not e:
        return None
    protected = e["state"] in (STATE_PAID, STATE_RESHIPPED)
    return {"order_id": e["order_id"], "holding_days": e["holding_days"],
            "returned_at": e["returned_at"],
            "abandon_at": None if protected else e["abandon_at"],
            "days_remaining": None if protected else e["days_remaining"],
            "final_period": e["final_period"] and not protected,
            "abandoned_at": e["abandoned_at"],
            "will_be_abandoned": not protected and e["state"] == STATE_RETURNED,
            "abandoned": e["state"] == STATE_ABANDONED}


# ---------------------------------------------------------------- prompt

def _line(e):
    parts = ["order %s: state=%s" % (e["order_id"], e["state"])]
    if e["sub_state"]:
        parts.append("sub_state=%s" % e["sub_state"])
    parts.append("can_pay=%s" % ("yes" if e["can_pay"] else "no"))
    if e["state"] == STATE_RETURNING:
        parts.append("(courier is bringing it back; not received by Optiwar yet)")
        if e["original_awb"]:
            parts.append("returning_awb=%s (%s)" % (e["original_awb"], e["original_courier"]))
    if e["returned_at"] and e["state"] != STATE_RETURNING:
        parts.append("received_by_ops=%s" % _fmt(e["returned_at"]))
    if e["state"] == STATE_RETURNED:
        if e["abandon_at"]:
            parts.append("abandon_at=%s" % _fmt(e["abandon_at"]))
        if e["days_remaining"] is not None:
            parts.append("days_remaining=%s" % e["days_remaining"])
        if e["on_hold"]:
            parts.append("(payment paused while our team reviews; do not offer payment)")
    if e["state"] in (STATE_PAID, STATE_RESHIPPED):
        parts.append("fee_paid=yes")
        if e["paid_at"]:
            parts.append("paid_on=%s" % _fmt(e["paid_at"]))
        parts.append("(a paid parcel is never abandoned)")
    if e["state"] == STATE_PAID:
        parts.append("(Ops is preparing the reshipment; no new AWB yet)")
    if e["state"] == STATE_RESHIPPED:
        if e["new_awb"]:
            parts.append("new_awb=%s (%s)" % (e["new_awb"], e["new_courier"] or "courier"))
            if e["new_track_url"]:
                parts.append("track=%s" % e["new_track_url"])
        if e["reshipped_at"]:
            parts.append("reshipped_on=%s" % _fmt(e["reshipped_at"]))
    if e["state"] == STATE_ABANDONED:
        parts.append("abandoned_on=%s" % (_fmt(e["abandoned_at"]) or "n/a"))
        parts.append("(no longer reshippable online; refer to %s)" % reship.SUPPORT_EMAIL)
    return "  " + " ".join(parts)


def prompt_section(model):
    """The system-prompt section: rules, then this customer's facts."""
    head = RULES % {"path": MY_ORDERS_PATH, "days": model["holding_days"],
                    "support": model["support"]}
    if not model["orders"]:
        body = ("\nCUSTOMER'S RETURNED PARCELS: none on record. If asked, say there is no "
                "returned-parcel record and offer a support ticket.")
    else:
        body = "\nCUSTOMER'S RETURNED PARCELS (authoritative):\n" + "\n".join(
            _line(e) for e in model["orders"])
    return "\n\n" + head + body + "\n"


# ---------------------------------------------------------------- reply check
# The rules above are what the model is told; these are the few of them a
# reply can be checked against mechanically. A hit is a defect record for the
# QC layer, never a rewrite of the reply.

_PAY_NOW = re.compile(r"\b(?:can|may|able to|please|kindly|go ahead and|proceed to)\s+"
                      r"(?:now\s+)?(?:pay|make the payment|complete the payment)\b|"
                      r"\bpay(?:ment)?\s+(?:the\s+)?(?:inr\s*|rs\.?\s*|₹\s*)?250\b(?!.*\b(?:cannot|can't|not yet|once|when|after)\b)",
                      re.I)
_PAY_AGAIN = re.compile(r"\b(?:pay|payment)\b.*\b(?:again|once more|another)\b|"
                        r"\b(?:again|another)\b.*\b(?:pay|payment)\b", re.I)
_ABANDON_CLAIM = re.compile(r"\b(?:will be|is|been|be treated as|marked as)\s+"
                            r"(?:treated as\s+)?abandoned\b", re.I)
_AWB_LIKE = re.compile(r"\b(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{7,}\b")
_AWB_CONTEXT = re.compile(r"\b(?:awb|tracking|consignment|waybill)\b", re.I)
_NOT_PAYABLE = re.compile(r"\b(?:cannot|can't|can not|not (?:yet|possible|available|able)|"
                          r"isn't|is not)\b.{0,60}\bpay", re.I)

V_PAY_NOT_PAYABLE = "RESHIP_PAY_OFFERED_WHILE_NOT_PAYABLE"
V_PAY_AGAIN = "RESHIP_PAY_REQUESTED_WHEN_PAID"
V_ABANDON_PAID = "RESHIP_ABANDON_CLAIMED_WHEN_PAID"
V_AWB_INVENTED = "RESHIP_AWB_NOT_IN_LEDGER"


def reply_violations(model, reply):
    """Codes for the ledger rules this reply breaks; empty when it keeps them.

    Checked only for a customer who is in the workflow (``model['orders']``
    non-empty) — outside it the reply has nothing to contradict."""
    text = reply or ""
    orders = model.get("orders") or []
    if not orders or not text.strip():
        return []
    out = []
    payable = any(e["can_pay"] for e in orders)
    paid = any(e["state"] in (STATE_PAID, STATE_RESHIPPED) for e in orders)
    unpaid_open = any(e["state"] in (STATE_RETURNING, STATE_RETURNED) for e in orders)
    if not payable and _PAY_NOW.search(text) and not _NOT_PAYABLE.search(text):
        out.append(V_PAY_NOT_PAYABLE)
    if paid and not unpaid_open and _PAY_AGAIN.search(text):
        out.append(V_PAY_AGAIN)
    if paid and not unpaid_open and _ABANDON_CLAIM.search(text) \
            and not re.search(r"\bnever\b|\bnot\b.{0,20}\babandoned", text, re.I):
        out.append(V_ABANDON_PAID)
    if _AWB_CONTEXT.search(text):
        known = {str(e[k]).upper() for e in orders for k in ("new_awb", "original_awb", "order_id")
                 if e.get(k)}
        for tok in _AWB_LIKE.findall(text.upper()):
            if tok not in known and tok not in {"OPTIWAR", "TRACKING"}:
                out.append(V_AWB_INVENTED)
                break
    return out


# ---------------------------------------------------------------- KET

KET_FIELDS = ("order_id", "state", "sub_state", "returned_at", "abandon_at", "days_remaining",
              "on_hold", "fee_paid", "paid_at", "reship_uuid", "original_awb", "new_awb",
              "reshipped_at", "abandoned_at", "notified")


def ket_context(db, model):
    """The safe structured snapshot a support ticket carries: no provider ids,
    tokens or URLs — only what the ledger says at the moment of escalation."""
    uuids = [e["reship_uuid"] for e in model["orders"] if e["reship_uuid"]]
    notes = {}
    if uuids:
        try:
            notes = reship.notifications_for(db, uuids)
        except Exception:  # noqa: BLE001 - the ticket must still be created
            notes = {}
    out = []
    for e in model["orders"]:
        n = notes.get(e["reship_uuid"]) or []
        out.append({
            "order_id": e["order_id"], "state": e["state"], "sub_state": e["sub_state"],
            "returned_at": _fmt(e["returned_at"]), "abandon_at": _fmt(e["abandon_at"]),
            "days_remaining": e["days_remaining"], "on_hold": e["on_hold"],
            "fee_paid": e["state"] in (STATE_PAID, STATE_RESHIPPED),
            "paid_at": _fmt(e["paid_at"]), "reship_uuid": e["reship_uuid"],
            "original_awb": e["original_awb"], "new_awb": e["new_awb"],
            "reshipped_at": _fmt(e["reshipped_at"]), "abandoned_at": _fmt(e["abandoned_at"]),
            "notified": sorted({"%s/%s" % (ev, ch) for ev, ch, _, failed in n
                                if ev and not failed}) or None,
        })
    return out


def ket_context_text(entries):
    if not entries:
        return ""
    lines = ["", "Returned-parcel context (snapshot at escalation):"]
    for e in entries:
        lines.append("- " + "; ".join("%s=%s" % (k, e[k]) for k in KET_FIELDS
                                      if e.get(k) not in (None, "", [], False)))
    return "\n".join(lines) + "\n"
