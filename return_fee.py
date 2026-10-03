"""The ₹250 reverse-pickup fee, paid by the customer on its own Razorpay order
once Ops approved the return request (reverse pickup, phase 3b).

The server decides amount, currency, receipt and notes; the browser only
names the order. A payment is applied from Razorpay's own record of it
(fetched after a valid checkout signature, delivered by the signed webhook,
or found by the reconcile cron), exactly once, to the one case whose
Razorpay order it was made against. It never reaches merchandise
settlement."""

try:
    from . import reship
    from . import reverse_pickup as rp
    from .paid_orders import add_history
except ImportError:  # pragma: no cover - flat test imports
    import reship
    import reverse_pickup as rp
    from paid_orders import add_history

PURPOSE = "REVERSE_PICKUP_FEE"
RECEIPT_PREFIX = "rpfee_"

# Internal audit only; Ops receives rp.EV_FEE_PAID.
EV_PAYMENT_STARTED = "reverse_pickup.fee_payment_started"
EV_PAYMENT_REFUSED = "reverse_pickup.fee_payment_refused"

APPLIED = reship.APPLIED
DUPLICATE = reship.DUPLICATE
NOT_CAPTURED = reship.NOT_CAPTURED
ORDER_MISMATCH = reship.ORDER_MISMATCH
AMOUNT_MISMATCH = reship.AMOUNT_MISMATCH
CURRENCY_MISMATCH = reship.CURRENCY_MISMATCH
ALREADY_BOUND = reship.ALREADY_BOUND
NOT_PAYABLE = reship.NOT_PAYABLE
UNKNOWN_CASE = "unknown_case"

PAID_STATES = (rp.FEE_PAID, rp.FEE_REFUNDED, rp.FEE_PARTIALLY_REFUNDED)


def payable_refusal(case):
    """Why this case's fee cannot be paid by the customer now, or None."""
    if not case or case.get("request_status") != rp.REQ_APPROVED:
        return ("not_approved", "This return has not been approved for payment")
    if case["fee_state"] in PAID_STATES:
        return ("already_paid", "The reverse-pickup fee is already paid")
    if case["fee_state"] == rp.FEE_WAIVED:
        return ("fee_waived", "The reverse-pickup fee has been waived; nothing is due")
    if case["fee_state"] != rp.FEE_DUE or case.get("completed_at") or case.get("received_at"):
        return ("not_payable", "This return is no longer awaiting the fee")
    return None


def customer_case(db, customer_id, order_id, host, environ=None):
    if not (rp.enabled(environ) and rp.customer_enabled(environ) and reship.is_india_host(host)):
        raise rp.ReversePickupError("not_found", "Not found", 404)
    rp.ensure_schema(db)
    oid = rp.resolve_order_id(db, order_id)
    case = rp.case_for_order(db, oid) if oid else None
    head = reship._order_head(db.cursor(), oid) if case else {}
    db.commit()
    if (not case or not customer_id or str(case.get("customer_id")) != str(customer_id)
            or str(head.get("customer_id")) != str(customer_id) or head.get("is_test")
            or not reship.is_india_host(head.get("site_from"))):
        raise rp.ReversePickupError("not_found", "Not found", 404)
    return oid, case


def begin_payment(db, customer_id, order_id, host, create_order, environ=None, logger=None):
    """Create, or hand back, the one Razorpay order for this case's fee.
    Returns ``(case, created)``."""
    oid, case = customer_case(db, customer_id, order_id, host, environ)
    refusal = payable_refusal(case)
    if refusal:
        raise rp.ReversePickupError(refusal[0], refusal[1], 409)
    locked = rp.case_by_uuid_for_update(db, case["case_uuid"])
    refusal = payable_refusal(locked)
    if refusal:
        db.rollback()
        raise rp.ReversePickupError(refusal[0], refusal[1], 409)
    if locked["razorpay_order_id"]:
        db.commit()
        return locked, False
    amount, currency = int(locked["fee_amount_minor"]), locked["fee_currency"]
    receipt = RECEIPT_PREFIX + locked["case_uuid"].replace("-", "")[:24]
    notes = {"purpose": PURPOSE, "case_uuid": locked["case_uuid"], "original_order_id": oid,
             "amount": str(amount), "currency": currency, "optiwar_host": host or ""}
    try:
        rzp = create_order(amount, currency, receipt, notes)
    except Exception:
        db.rollback()
        raise
    if (int(rzp.get("amount") or 0) != amount or (rzp.get("currency") or "") != currency
            or not rzp.get("id")):
        db.rollback()
        raise rp.ReversePickupError("provider_mismatch", "Payment could not be started", 502)
    cur = db.cursor()
    cur.execute("UPDATE reverse_pickup_cases SET razorpay_order_id=%s WHERE id=%s AND fee_state=%s "
                "AND razorpay_order_id IS NULL", (rzp["id"], locked["id"], rp.FEE_DUE))
    rp._audit(db, EV_PAYMENT_STARTED, locked,
              {"razorpay_order_id": rzp["id"], "amount": amount, "currency": currency})
    db.commit()
    if logger:
        logger.info("ACTIVITY:REVERSE_PICKUP_FEE_PAYMENT_STARTED order:%s case:%s rzp_order:%s"
                    % (oid, locked["case_uuid"], rzp["id"]))
    return rp.case_by_uuid(db, locked["case_uuid"]), True


def is_fee_payment(payment):
    notes = (payment or {}).get("notes") or {}
    return isinstance(notes, dict) and (notes.get("purpose") or "") == PURPOSE


def case_uuid_of(payment):
    notes = (payment or {}).get("notes") or {}
    return (notes.get("case_uuid") or "").strip() if isinstance(notes, dict) else ""


def case_for_payment(db, payment):
    """The case uuid a Razorpay payment belongs to, or ''. Decided by the
    payment's Razorpay order against the order the case stored; the notes
    only corroborate, or name the case of a payment whose order is unknown."""
    rp.ensure_schema(db)
    rzp_order = ((payment or {}).get("order_id") or "").strip()
    if rzp_order:
        cur = db.cursor()
        cur.execute("SELECT case_uuid FROM reverse_pickup_cases WHERE razorpay_order_id=%s LIMIT 1",
                    (rzp_order,))
        row = cur.fetchone()
        db.commit()
        if row:
            return row["case_uuid"]
    if is_fee_payment(payment):
        return case_uuid_of(payment)
    return ""


def _bound_elsewhere(cur, payment_id, case_id):
    cur.execute("SELECT order_id FROM payment_collector WHERE payment_ref=%s "
                "AND status='TXN_SUCCESS' LIMIT 1", (payment_id,))
    row = cur.fetchone()
    if row:
        return "order " + (row.get("order_id") or "")
    cur.execute("SELECT reship_uuid FROM order_reshipments WHERE razorpay_payment_id=%s LIMIT 1",
                (payment_id,))
    row = cur.fetchone()
    if row:
        return "reship " + (row.get("reship_uuid") or "")
    cur.execute("SELECT case_uuid FROM reverse_pickup_cases WHERE razorpay_payment_id=%s AND id<>%s "
                "LIMIT 1", (payment_id, case_id))
    row = cur.fetchone()
    if row:
        return "reverse-pickup case " + row["case_uuid"]
    return ""


def settle(db, case_uuid, payment, source, logger=None):
    """Apply Razorpay's record of the fee payment to one case, once.
    Returns ``{'outcome', 'reason', 'case'}``; anything but APPLIED or
    DUPLICATE leaves the case untouched. The customer's fee-received notice
    is owed in the same transaction; the caller sends it."""
    rp.ensure_schema(db)
    payment = payment or {}
    payment_id = (payment.get("id") or "").strip()
    out = {"outcome": None, "reason": "", "case": None, "case_uuid": case_uuid}

    def refuse(outcome, reason, history=None):
        db.rollback()
        out["outcome"], out["reason"] = outcome, reason
        if logger:
            logger.error("ACTIVITY:REVERSE_PICKUP_FEE_REFUSED case:%s payment:%s source:%s "
                         "outcome:%s %s" % (case_uuid, payment_id, source, outcome, reason))
        case = out["case"]
        if case and outcome != NOT_CAPTURED:
            rp._audit(db, EV_PAYMENT_REFUSED, case,
                      {"outcome": outcome, "reason": reason, "payment_id": payment_id,
                       "source": source}, suffix=payment_id)
            if history:
                add_history(db.cursor(), case["order_id"], history, case.get("site_from"))
            db.commit()
        return out

    if not payment_id:
        return refuse(UNKNOWN_CASE, "no payment id")
    if (payment.get("status") or "") != "captured":
        return refuse(NOT_CAPTURED, "status %s" % payment.get("status"))
    case = rp.case_by_uuid_for_update(db, case_uuid)
    out["case"] = case
    if not case:
        return refuse(UNKNOWN_CASE, "no such case")
    if case["razorpay_payment_id"] == payment_id and case["fee_state"] in PAID_STATES:
        db.commit()
        out["outcome"] = DUPLICATE
        return out
    if (payment.get("order_id") or "") != (case["razorpay_order_id"] or "~"):
        return refuse(ORDER_MISMATCH, "payment is for razorpay order %s, case holds %s"
                      % (payment.get("order_id"), case["razorpay_order_id"]))
    if case_uuid_of(payment) and case_uuid_of(payment) != case_uuid:
        return refuse(ORDER_MISMATCH, "payment notes name another case")
    if int(payment.get("amount") or 0) != int(case["fee_amount_minor"]):
        return refuse(AMOUNT_MISMATCH, "paid %s, fee %s" % (payment.get("amount"),
                                                             case["fee_amount_minor"]))
    if (payment.get("currency") or "") != case["fee_currency"]:
        return refuse(CURRENCY_MISMATCH, "paid in %s, fee in %s"
                      % (payment.get("currency"), case["fee_currency"]))
    cur = db.cursor()
    elsewhere = _bound_elsewhere(cur, payment_id, case["id"])
    if elsewhere:
        return refuse(ALREADY_BOUND, "payment already paid " + elsewhere)
    if case["fee_state"] != rp.FEE_DUE:
        return refuse(NOT_PAYABLE, "fee is %s" % case["fee_state"],
                      "PAYMENT EXCEPTION: reverse-pickup fee INR %d captured (razorpay %s) while the "
                      "fee is %s - refund by hand" % (int(case["fee_amount_minor"]) // 100,
                                                       payment_id, case["fee_state"]))
    try:
        cur.execute("UPDATE reverse_pickup_cases SET fee_state=%s, razorpay_payment_id=%s, "
                    "fee_paid_at=NOW() WHERE id=%s AND fee_state=%s",
                    (rp.FEE_PAID, payment_id, case["id"], rp.FEE_DUE))
    except Exception as exc:  # noqa: BLE001
        if rp._is_duplicate_key(exc):
            return refuse(ALREADY_BOUND, "payment id already bound")
        db.rollback()
        raise
    if cur.rowcount != 1:
        db.rollback()
        out["outcome"] = DUPLICATE
        return out
    paid = rp.case_by_uuid(db, case_uuid)
    amount = int(paid["fee_amount_minor"])
    add_history(cur, paid["order_id"], "Reverse-pickup fee INR %d received - razorpay %s (%s)"
                % (amount // 100, payment_id, source), paid.get("site_from"))
    data = {"razorpay_payment_id": payment_id, "amount_minor": amount,
            "currency": paid["fee_currency"], "source": source,
            "paid_at": rp._iso(paid.get("fee_paid_at"))}
    rp._audit(db, rp.EV_FEE_PAID, paid, data)
    rp.queue_ops_event(db, rp.EV_FEE_PAID, paid["order_id"], data, case=paid,
                       pickup=rp.latest_for_order(db, paid["order_id"]), key=case_uuid, commit=False)
    rp.enqueue_notice(db, paid, rp.NOTICE_FEE_PAID)
    db.commit()
    if logger:
        logger.info("ACTIVITY:REVERSE_PICKUP_FEE_PAID order:%s case:%s payment:%s source:%s"
                    % (paid["order_id"], case_uuid, payment_id, source))
    out["outcome"], out["case"] = APPLIED, paid
    return out


def reconcile_pending(db, fetch_order_payments, logger=None, limit=200):
    """For every case with a fee order and the fee still DUE, ask Razorpay.
    A captured payment goes through ``settle`` like any other; a provider
    failure is skipped and retried next run."""
    rp.ensure_schema(db)
    cur = db.cursor()
    cur.execute("SELECT case_uuid, razorpay_order_id FROM reverse_pickup_cases WHERE fee_state=%s "
                "AND razorpay_order_id IS NOT NULL ORDER BY id LIMIT %s", (rp.FEE_DUE, int(limit)))
    rows = cur.fetchall()
    db.commit()
    summary = {"checked": len(rows), "settled": [], "unpaid": 0, "refused": [], "unavailable": 0}
    for r in rows:
        try:
            payments = fetch_order_payments(r["razorpay_order_id"]) or []
        except Exception as exc:  # noqa: BLE001
            summary["unavailable"] += 1
            if logger:
                logger.warning("REVERSE_PICKUP_FEE_RECONCILE_UNAVAILABLE case:%s %s"
                               % (r["case_uuid"], str(exc)[:120]))
            continue
        captured = [p for p in payments if (p.get("status") or "") == "captured"]
        if not captured:
            summary["unpaid"] += 1
            continue
        res = settle(db, r["case_uuid"], captured[0], "razorpay-reconcile", logger=logger)
        if res["outcome"] == APPLIED:
            summary["settled"].append(r["case_uuid"])
        elif res["outcome"] != DUPLICATE:
            summary["refused"].append({"case_uuid": r["case_uuid"], "outcome": res["outcome"]})
    return summary
