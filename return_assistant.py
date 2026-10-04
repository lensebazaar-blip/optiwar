"""What the assistant may know and say about a customer's return (India
reverse pickup).

Every fact is read from Optiwar's return record — ``reverse_pickup_cases``
and ``order_reverse_pickups`` — the same rows the Ops API and the My Orders
card are built from. The model receives the facts as a prompt section and a
set of rules; it receives no write path. Nothing Ops typed (operator, notes,
remarks, condition, waiver note) and no payment-provider id reaches it.

Stages, in the order a return moves through them:

    FEE_DUE                       case open, the INR 250 fee not yet settled
    PICKUP_TO_BE_BOOKED           fee PAID or WAIVED; Ops books the pickup
    PICKUP_BOOKED                 Delhivery reverse waybill recorded
    PICKUP_CANCELLED              the latest waybill was cancelled
    RECEIVED_AWAITING_INSPECTION  Ops has the parcel
    INSPECTED_NO_DEFECT           the reported defect was not confirmed;
                                  the customer is asked to reply by email
    INSPECTED_DEFECT_CONFIRMED    a person confirmed the defect
    CONSENT_RECEIVED              the customer's emailed reply is recorded
    SHIPPED_TO_CUSTOMER           Ops shipped the product (replacement, repaired
                                  original, or the original) back to the customer
    COMPLETED                     Ops closed the case with its outcome
"""
import re

try:
    from . import reship
    from . import reverse_pickup as rp
except ImportError:  # pragma: no cover - flat test import
    import reship
    import reverse_pickup as rp

MY_ORDERS_PATH = "/profile/?tab=orders"
MAX_ORDERS = 30

ST_FEE_DUE = "FEE_DUE"
ST_TO_BOOK = "PICKUP_TO_BE_BOOKED"
ST_BOOKED = "PICKUP_BOOKED"
ST_CANCELLED = "PICKUP_CANCELLED"
ST_RECEIVED = "RECEIVED_AWAITING_INSPECTION"
ST_NO_DEFECT = "INSPECTED_NO_DEFECT"
ST_DEFECT = "INSPECTED_DEFECT_CONFIRMED"
ST_CONSENT = "CONSENT_RECEIVED"
ST_SHIPPED = "SHIPPED_TO_CUSTOMER"
ST_COMPLETED = "COMPLETED"
ST_ABANDONED = "RETURN_ABANDONED"
ST_REQUESTED = "RETURN_REQUESTED"
ST_INFO = "RETURN_INFO_REQUESTED"
ST_NOT_APPROVED = "RETURN_NOT_APPROVED"
PRE_PICKUP = (ST_FEE_DUE, ST_TO_BOOK, ST_REQUESTED, ST_INFO, ST_NOT_APPROVED)

FEE_REFUNDED_STATES = (rp.FEE_REFUNDED, rp.FEE_PARTIALLY_REFUNDED)

RULES = """RETURN / REVERSE-PICKUP RULES (India only; authoritative, from Optiwar's return record):
- A return is collected by a Delhivery reverse pickup that our team books. The reverse-pickup fee is
  a fixed INR %(fee)s (clause 9B of the Terms). Never quote another amount, and never offer to
  waive, change or refund it: only our team decides that.
- RETURN_REQUESTED: the customer's return request is with our team for review; they reply by
  email within 2 working days. No fee is due yet and no pickup is booked; the customer must not
  send the product. RETURN_INFO_REQUESTED: our team emailed asking for more details or photos;
  the customer replies to that email. RETURN_NOT_APPROVED: our team did not approve the return
  and emailed the reason; offer a support ticket for questions. Never approve, decline or promise
  approval of a request yourself.
- A pickup is booked only after the fee is PAID or WAIVED. If stage=FEE_DUE, say the INR %(fee)s fee
  must be settled before the pickup can be booked. If the line says pay_in_my_orders, the customer
  pays it with the Pay button on the return card in My Orders; otherwise our team will share how to
  pay. Give no payment link and do not say a pickup is booked.
- Give a pickup AWB or tracking link only when stage=PICKUP_BOOKED and pickup_awb is listed below.
  Never invent an AWB, a pickup date or a delivery date. If stage=PICKUP_CANCELLED, say that
  booking was cancelled and offer a support ticket.
- While a pickup is booked: keep the product packed in its original box with all accessories and
  the invoice, keep the phone reachable, and do not hand it to any other courier.
- RECEIVED_AWAITING_INSPECTION: we have the parcel; our team inspects it and emails the result;
  nothing is needed from the customer now.
- INSPECTED_NO_DEFECT: the inspection did not confirm the reported manufacturing defect. Where
  fee_state is PAID or DUE, the INR %(fee)s fee remains applicable. The customer was emailed and
  should reply to that email to have the product sent back; you cannot record that reply. Give the
  reply_by date and days_remaining exactly as listed below; after that date the product is treated
  as unclaimed under the Returns Policy. Never state a deadline that is not listed, and never extend
  it.
- RETURN_ABANDONED: the reply period ended without a reply and the return is closed; the product is
  treated as unclaimed under the Returns Policy. Say so plainly, do not promise it will be sent back
  or refunded, and offer a support ticket for the customer to ask our team.
- INSPECTED_DEFECT_CONFIRMED: the defect was confirmed; our team will update the customer on the
  resolution and the next shipment. Never promise a refund, an amount or a date.
- CONSENT_RECEIVED: the customer's reply is recorded; the product will be sent back and the
  shipment details will follow. Never invent a forward AWB.
- SHIPPED_TO_CUSTOMER: our team shipped the product back (shipped=REPLACEMENT, REPAIRED_ORIGINAL or
  ORIGINAL_RETURNED, as listed); give the forward_awb, courier and track link listed below, and
  nothing is payable for that shipment. Never invent a delivery date.
- COMPLETED: the return is closed with the outcome listed (REPLACEMENT_SHIPPED,
  REPAIRED_ORIGINAL_SHIPPED, ORIGINAL_RETURNED or PRODUCT_REFUNDED). Say a product refund was made
  only when outcome=PRODUCT_REFUNDED; never quote its amount or date.
- Say a fee was refunded only when fee_state is REFUNDED or PARTIALLY_REFUNDED below, with the
  amount listed.
- You cannot book or cancel a pickup, mark a parcel received, record an inspection or a reply,
  or take or refund the fee. For anything beyond these facts offer a support ticket (ask first,
  per the ticket rule).
- To take the customer to My Orders end with [ACTION:NAVIGATE:%(path)s] after offering.
- Answer only from the facts below. If an order is not listed, say you have no return record for
  it and offer to check with support."""


def enabled(host, environ=None):
    """The section exists only where the customer is told about returns:
    both reverse-pickup flags on, India host."""
    return (rp.enabled(environ) and rp.customer_enabled(environ)
            and reship.is_india_host(host))


def _fmt(d):
    if hasattr(d, "strftime"):
        return d.strftime("%d %b %Y")
    return str(d) if d else None


def stage(case, pickup):
    """The customer-facing stage of one order's return, or None when the order
    has no return record."""
    case = case or {}
    if case.get("completed_outcome") == rp.OUTCOME_NOT_APPROVED:
        return ST_NOT_APPROVED
    if case.get("completed_outcome") == rp.OUTCOME_ABANDONED:
        return ST_ABANDONED
    if case.get("completed_at"):
        return ST_COMPLETED
    if case.get("forward_shipped_at"):
        return ST_SHIPPED
    if case.get("request_status") == rp.REQ_SUBMITTED:
        return ST_REQUESTED
    if case.get("request_status") == rp.REQ_INFO:
        return ST_INFO
    if case.get("consent_at"):
        return ST_CONSENT
    if case.get("inspected_at"):
        return ST_DEFECT if case.get("inspection_defect") else ST_NO_DEFECT
    if case.get("received_at"):
        return ST_RECEIVED
    if pickup and pickup["status"] != rp.ST_CLOSED:
        return ST_BOOKED if pickup["status"] == rp.ST_BOOKED else ST_CANCELLED
    if not case:
        return None
    return ST_FEE_DUE if case["fee_state"] == rp.FEE_DUE else ST_TO_BOOK


def _india_orders(cur, customer_id):
    cur.execute("SELECT order_id, MIN(site_from) AS site_from, MAX(date_created) AS d "
                "FROM orders WHERE customer_id=%s AND (is_test IS NULL OR is_test=0) "
                "GROUP BY order_id ORDER BY d DESC LIMIT %s",
                (int(customer_id), MAX_ORDERS))
    return [r["order_id"] for r in cur.fetchall() if reship.is_india_host(r.get("site_from"))]


def _latest_by_order(cur, sql, order_ids):
    cur.execute(sql % ",".join(["%s"] * len(order_ids)), tuple(order_ids))
    out = {}
    for r in cur.fetchall():
        out[r["order_id"]] = r
    return out


def read_model(db, customer_id, host, environ=None):
    """Every return fact this customer may be told: one entry per order with a
    return record, newest order first. Scoped by the orders the customer owns,
    never by an id the request supplied. Read-only."""
    out = {"orders": [], "fee": rp.FEE_MINOR // 100, "currency": rp.FEE_CURRENCY}
    if not customer_id or not enabled(host, environ):
        return out
    cur = db.cursor()
    oids = _india_orders(cur, customer_id)
    if not oids:
        db.commit()
        return out
    cases = _latest_by_order(cur, "SELECT * FROM reverse_pickup_cases WHERE order_id IN (%s) "
                                  "ORDER BY case_no", oids)
    pickups = _latest_by_order(cur, "SELECT * FROM order_reverse_pickups WHERE order_id IN (%s) "
                                    "ORDER BY id", oids)
    db.commit()
    shipments = reship.shipments_for_orders(db, [o for o in oids if o in cases or o in pickups])
    for oid in oids:
        e = _entry(oid, cases.get(oid), pickups.get(oid), shipments.get(oid))
        if e:
            out["orders"].append(e)
    return out


def _entry(order_id, case, pickup, shipment):
    st = stage(case, pickup)
    if st is None:
        return None
    case = case or {}
    pv = rp.public_view(pickup)
    fv = rp.forward_view(case)
    if pickup and pickup.get("forward_awb"):
        shipment = (pickup["forward_awb"], shipment[1] if shipment else "")
    fee_state = case.get("fee_state")
    refunded = int(case.get("fee_refunded_minor") or 0) // 100
    hold = rp.hold_view(case) or {}
    return {
        "order_id": order_id,
        "stage": st,
        "case_uuid": case.get("case_uuid"),
        "fee_state": fee_state,
        "fee": int(case.get("fee_amount_minor") or rp.FEE_MINOR) // 100,
        "fee_paid_at": case.get("fee_paid_at"),
        "pay_in_my_orders": (st == ST_FEE_DUE
                             and case.get("request_status") == rp.REQ_APPROVED),
        "fee_refunded": refunded if fee_state in FEE_REFUNDED_STATES else None,
        "pickup_status": pv["state"] if pv else None,
        "pickup_awb": pv["awb"] if pv else None,
        "pickup_courier": pv["courier"] if pv else None,
        "pickup_track_url": pv["track_url"] if pv else None,
        "pickup_booked_at": pv["booked_at"] if pv else None,
        "pickup_cancelled_at": pv["cancelled_at"] if pv else None,
        "received_at": case.get("received_at"),
        "inspected_at": case.get("inspected_at"),
        "manufacturing_defect": (bool(case["inspection_defect"])
                                 if case.get("inspected_at") else None),
        "consent_at": case.get("consent_at"),
        "completed_at": case.get("completed_at"),
        "completed_outcome": (case.get("completed_outcome")
                              if case.get("completed_at") else None),
        "shipped_type": fv["type"] if fv else None,
        "forward_awb": fv["awb"] if fv else None,
        "forward_courier": fv["courier"] if fv else None,
        "forward_track_url": fv["track_url"] if fv else None,
        "forward_shipped_at": case.get("forward_shipped_at") if fv else None,
        "original_awb": shipment[0] if shipment and shipment[0] else None,
        "original_courier": shipment[1] if shipment and shipment[0] else None,
        "reply_by": hold.get("abandon_at"),
        "reply_days_remaining": hold.get("days_remaining"),
        "holding_days": hold.get("holding_days"),
        "abandoned_at": hold.get("abandoned_at"),
    }


def _find(model, order_id):
    oid = str(order_id or "").strip().upper()
    if oid.startswith("OW-"):
        oid = oid[3:]
    for e in model["orders"]:
        if str(e["order_id"]).upper() == oid:
            return e
    return None


# ---------------------------------------------------------------- read tools
# Each answers one customer question from the model alone; ``None`` means
# "no return record for that order" and must be said as such.

def get_return_case_status(model, order_id):
    e = _find(model, order_id)
    if not e:
        return None
    return {"order_id": e["order_id"], "stage": e["stage"],
            "received": e["received_at"] is not None, "received_at": e["received_at"],
            "completed": e["stage"] == ST_COMPLETED,
            "abandoned": e["stage"] == ST_ABANDONED,
            "reply_by": e["reply_by"] if e["stage"] == ST_NO_DEFECT else None,
            "reply_days_remaining": e["reply_days_remaining"] if e["stage"] == ST_NO_DEFECT else None,
            "completed_outcome": e["completed_outcome"],
            "shipped_to_customer": ({"type": e["shipped_type"], "awb": e["forward_awb"],
                                     "courier": e["forward_courier"],
                                     "track_url": e["forward_track_url"],
                                     "shipped_at": e["forward_shipped_at"]}
                                    if e["forward_awb"] else None)}


def get_reverse_pickup_tracking(model, order_id):
    e = _find(model, order_id)
    if not e:
        return None
    booked = e["stage"] == ST_BOOKED and bool(e["pickup_awb"])
    return {"order_id": e["order_id"], "booked": booked,
            "cancelled": e["stage"] == ST_CANCELLED,
            "awb": e["pickup_awb"] if booked else None,
            "courier": e["pickup_courier"] if booked else None,
            "track_url": e["pickup_track_url"] if booked else None,
            "booked_at": e["pickup_booked_at"] if booked else None}


def get_return_fee_status(model, order_id):
    e = _find(model, order_id)
    if not e:
        return None
    return {"order_id": e["order_id"], "fee": e["fee"], "currency": model["currency"],
            "fee_state": e["fee_state"], "paid_at": e["fee_paid_at"],
            "refunded": e["fee_refunded"],
            "settled": e["fee_state"] in rp.FEE_SETTLED + FEE_REFUNDED_STATES}


def get_return_inspection_status(model, order_id):
    e = _find(model, order_id)
    if not e:
        return None
    inspected = e["inspected_at"] is not None
    return {"order_id": e["order_id"], "inspected": inspected,
            "inspected_at": e["inspected_at"],
            "manufacturing_defect": e["manufacturing_defect"],
            "awaiting_customer_reply": e["stage"] == ST_NO_DEFECT,
            "customer_reply_recorded": e["consent_at"] is not None}


# ---------------------------------------------------------------- prompt

def _line(e, fee):
    parts = ["order %s: stage=%s" % (e["order_id"], e["stage"])]
    if e["fee_state"]:
        parts.append("fee_state=%s" % e["fee_state"])
        if e["fee_paid_at"]:
            parts.append("fee_paid_on=%s" % _fmt(e["fee_paid_at"]))
        if e["fee_refunded"] is not None:
            parts.append("fee_refunded=INR %s" % e["fee_refunded"])
    else:
        parts.append("fee_state=not recorded")
    if e["stage"] == ST_BOOKED and e["pickup_awb"]:
        parts.append("pickup_awb=%s (%s)" % (e["pickup_awb"], e["pickup_courier"]))
        if e["pickup_track_url"]:
            parts.append("track=%s" % e["pickup_track_url"])
        if e["pickup_booked_at"]:
            parts.append("booked_on=%s" % _fmt(e["pickup_booked_at"]))
    if e["stage"] == ST_CANCELLED:
        parts.append("(the pickup booking was cancelled; no agent will come for it)")
    if e["stage"] == ST_REQUESTED:
        parts.append("(return request under review; no fee due yet; no pickup yet)")
    if e["stage"] == ST_INFO:
        parts.append("(our team emailed asking for more information; no pickup yet)")
    if e["stage"] == ST_NOT_APPROVED:
        parts.append("(return request not approved; the customer was emailed the reason)")
    if e["stage"] == ST_FEE_DUE:
        parts.append("(INR %s fee due before a pickup can be booked; no pickup yet)" % fee)
        if e.get("pay_in_my_orders"):
            parts.append("pay_in_my_orders")
    if e["stage"] == ST_TO_BOOK:
        parts.append("(fee settled; our team books the pickup; no AWB yet)")
    if e["received_at"]:
        parts.append("received_by_optiwar=%s" % _fmt(e["received_at"]))
    if e["inspected_at"]:
        parts.append("inspected_on=%s manufacturing_defect=%s"
                     % (_fmt(e["inspected_at"]), "confirmed" if e["manufacturing_defect"]
                        else "not confirmed"))
    if e["stage"] == ST_NO_DEFECT:
        parts.append("(customer was emailed; they reply to that email to have it sent back)")
        if e["reply_by"]:
            parts.append("reply_by=%s days_remaining=%s" % (_fmt(e["reply_by"]), e["reply_days_remaining"]))
    if e["stage"] == ST_ABANDONED:
        parts.append("(no reply within %s days of the inspection; return closed on %s; the product is "
                     "treated as unclaimed)" % (e["holding_days"], _fmt(e["abandoned_at"])))
    if e["consent_at"]:
        parts.append("customer_reply_recorded=%s (product to be sent back; details to follow)"
                     % _fmt(e["consent_at"]))
    if e["forward_awb"]:
        parts.append("shipped=%s forward_awb=%s (%s)" % (e["shipped_type"], e["forward_awb"],
                                                         e["forward_courier"]))
        if e["forward_track_url"]:
            parts.append("track=%s" % e["forward_track_url"])
        if e["forward_shipped_at"]:
            parts.append("shipped_on=%s" % _fmt(e["forward_shipped_at"]))
    if e["completed_at"]:
        parts.append("completed_on=%s outcome=%s" % (_fmt(e["completed_at"]), e["completed_outcome"]))
    return "  " + " ".join(parts)


def prompt_section(model):
    """The system-prompt section: rules, then this customer's facts."""
    head = RULES % {"fee": model["fee"], "path": MY_ORDERS_PATH}
    if not model["orders"]:
        body = ("\nCUSTOMER'S RETURNS: none on record. If asked, say there is no return record "
                "and offer a support ticket.")
    else:
        body = "\nCUSTOMER'S RETURNS (authoritative):\n" + "\n".join(
            _line(e, model["fee"]) for e in model["orders"])
    return "\n\n" + head + body + "\n"


# ---------------------------------------------------------------- reply check
# The few rules a reply can be checked against mechanically. A hit is a
# defect record for the QC layer, never a rewrite of the reply.

_AWB_LIKE = re.compile(r"\b(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{7,}\b")
_AWB_CONTEXT = re.compile(r"\b(?:awb|tracking|consignment|waybill)\b", re.I)
_REFUND_PROMISE = re.compile(
    r"\b(?:will|shall|going to)\s+(?:be\s+)?(?:refund(?:ed)?|get\s+(?:a\s+|your\s+|the\s+)?refund)\b|"
    r"\brefund\s+(?:will be|is being|has been)\s+(?:processed|initiated|issued|credited|made)\b|"
    r"\b(?:we|i)\s+(?:will|shall|have)\s+(?:refund|refunded)\b", re.I)
_PICKUP_BOOKED_CLAIM = re.compile(
    r"\bpick-?up\b.{0,40}\b(?:is|has been|was|been)\s+(?:booked|scheduled|arranged|confirmed)\b|"
    r"\b(?:booked|scheduled|arranged)\s+(?:a|your|the)\s+(?:reverse\s+)?pick-?up\b", re.I)
_NEGATED = re.compile(r"\b(?:not|no|cannot|can't|isn't|hasn't|yet to|once|after|before|until)\b", re.I)

V_AWB_INVENTED = "RETURN_AWB_NOT_IN_RECORD"
V_REFUND_PROMISED = "RETURN_REFUND_PROMISED"
V_PICKUP_BEFORE_FEE = "RETURN_PICKUP_CLAIMED_BEFORE_FEE"


def _sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _claims(pattern, text):
    """A sentence that states the claim without qualifying it."""
    return any(pattern.search(s) and not _NEGATED.search(s) for s in _sentences(text))


def reply_violations(model, reply):
    """Codes for the record rules this reply breaks; empty when it keeps them.
    Checked only for a customer with a return on record."""
    text = reply or ""
    orders = model.get("orders") or []
    if not orders or not text.strip():
        return []
    out = []
    if _AWB_CONTEXT.search(text):
        known = {str(e[k]).upper() for e in orders
                 for k in ("pickup_awb", "original_awb", "forward_awb", "order_id") if e.get(k)}
        known |= {"OW-" + str(e["order_id"]).upper() for e in orders}
        for tok in _AWB_LIKE.findall(text.upper()):
            if tok not in known and tok not in {"OPTIWAR", "TRACKING", "DELHIVERY"}:
                out.append(V_AWB_INVENTED)
                break
    refunded = any(e["fee_state"] in FEE_REFUNDED_STATES for e in orders)
    if not refunded and _claims(_REFUND_PROMISE, text):
        out.append(V_REFUND_PROMISED)
    booked = any(e["stage"] not in PRE_PICKUP for e in orders)
    if not booked and _claims(_PICKUP_BOOKED_CLAIM, text):
        out.append(V_PICKUP_BEFORE_FEE)
    return out


# ---------------------------------------------------------------- KET

KET_FIELDS = ("order_id", "stage", "fee_state", "fee_refunded", "case_uuid", "pickup_status",
              "pickup_awb", "received_at", "inspected_at", "manufacturing_defect", "reply_by",
              "consent_at", "forward_awb", "completed_at", "completed_outcome", "abandoned_at")


def ket_context(model):
    """The structured snapshot a support ticket carries: stages, fee state and
    AWBs only — no operator, note, remark or provider id."""
    out = []
    for e in model.get("orders") or []:
        out.append({
            "order_id": e["order_id"], "stage": e["stage"], "fee_state": e["fee_state"],
            "fee_refunded": e["fee_refunded"], "case_uuid": e["case_uuid"],
            "pickup_status": e["pickup_status"], "pickup_awb": e["pickup_awb"],
            "received_at": _fmt(e["received_at"]), "inspected_at": _fmt(e["inspected_at"]),
            "manufacturing_defect": ({True: "confirmed", False: "not confirmed"}
                                     .get(e["manufacturing_defect"])),
            "consent_at": _fmt(e["consent_at"]), "forward_awb": e["forward_awb"],
            "completed_at": _fmt(e["completed_at"]), "completed_outcome": e["completed_outcome"],
            "reply_by": _fmt(e["reply_by"]) if e["stage"] == ST_NO_DEFECT else None,
            "abandoned_at": _fmt(e["abandoned_at"]),
        })
    return out


def ket_context_text(entries):
    if not entries:
        return ""
    lines = ["", "Return / reverse-pickup context (snapshot at escalation):"]
    for e in entries:
        lines.append("- " + "; ".join("%s=%s" % (k, e[k]) for k in KET_FIELDS
                                      if e.get(k) not in (None, "", [])))
    return "\n".join(lines) + "\n"
