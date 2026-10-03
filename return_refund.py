"""The automatic refund of a reverse-pickup fee (phase 3c).

When Ops records a confirmed manufacturing defect on a case whose ₹250 fee is
PAID, ``reverse_pickup.record_inspection`` marks the refund PENDING in the same
transaction. Nothing else ever asks for this refund, and it is executed here,
by the inspection route straight away and by the 15-minute reconcile job until
Razorpay accepts it:

    PENDING ─► REQUESTING ─► REFUNDED          (fee_state PAID ─► REFUNDED)
                   │
                   ├────────► FAILED  ─► retried after RETRY_MINUTES
                   └────────► EXCEPTION         (a person checks by hand)

Safeguards:

* only the case's own payment is refunded, for exactly ``fee_amount_minor``,
  after Razorpay itself says it is that captured payment of this case's
  Razorpay order in the case's currency;
* one idempotency key per case for its whole life
  (``reverse_pickup.refund_key``), sent as Razorpay's Idempotency-Key and in
  the refund's notes, so a retry after a timeout finds the first refund;
* a payment already refunded by anyone else is an EXCEPTION, never a second
  refund; Razorpay never refunds more than was captured in any case;
* a waived, unpaid or already refunded fee is never refunded;
* the refunded event, Ops event and customer notice are written once, in the
  transaction that records the refund; their delivery is retried on its own.

Off unless ``REVERSE_PICKUP_AUTO_REFUND_ENABLED``: while off a confirmed
defect stays REFUND PENDING (Ops queue, daily report) and Razorpay is not
called.
"""
import os

try:
    from . import reverse_pickup as rp
    from .paid_orders import add_history
except ImportError:  # pragma: no cover - flat import in scripts
    import reverse_pickup as rp
    from paid_orders import add_history

ENABLED_ENV = "REVERSE_PICKUP_AUTO_REFUND_ENABLED"
RETRY_MINUTES_ENV = "REVERSE_PICKUP_REFUND_RETRY_MINUTES"
RETRY_MINUTES = 15
# A REQUESTING claim this old was interrupted mid-call; the key makes it safe
# to ask again.
STALE_MINUTES = 30
PURPOSE = "REVERSE_PICKUP_FEE_REFUND"

REFUNDED = "refunded"
DUPLICATE = "duplicate"
DISABLED = "disabled"
NOT_ELIGIBLE = "not_eligible"
BUSY = "busy"
FAILED = "failed"
EXCEPTION = "exception"


def enabled(environ=None):
    env = os.environ if environ is None else environ
    return str(env.get(ENABLED_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def _clip(value, n=255):
    return str(value or "")[:n]


def _claim(db, case_uuid):
    """Lock the case and move it to REQUESTING. Returns ``(outcome, case)``;
    outcome None means the caller now owns the provider call."""
    case = rp.case_by_uuid_for_update(db, case_uuid)
    if not case:
        db.rollback()
        return NOT_ELIGIBLE, None
    state = case.get("fee_refund_state")
    if case["fee_state"] == rp.FEE_REFUNDED or state == rp.REFUND_DONE:
        db.rollback()
        return DUPLICATE, case
    if state not in rp.REFUND_OPEN:
        db.rollback()
        return NOT_ELIGIBLE, case
    if state == rp.REFUND_REQUESTING:
        cur = db.cursor()
        cur.execute("SELECT fee_refund_last_attempt_at > NOW() - INTERVAL %s MINUTE AS busy "
                    "FROM reverse_pickup_cases WHERE id=%s", (STALE_MINUTES, case["id"]))
        if (cur.fetchone() or {}).get("busy"):
            db.rollback()
            return BUSY, case
    if (case["fee_state"] != rp.FEE_PAID or not case.get("razorpay_payment_id")
            or not case.get("inspection_defect")):
        _exception_locked(db, case, "case is not a PAID fee with a confirmed defect")
        return EXCEPTION, rp.case_by_uuid(db, case_uuid)
    cur = db.cursor()
    cur.execute("UPDATE reverse_pickup_cases SET fee_refund_state=%s, "
                "fee_refund_attempts=fee_refund_attempts+1, fee_refund_last_attempt_at=NOW(), "
                "fee_refund_key=COALESCE(fee_refund_key, %s) WHERE id=%s",
                (rp.REFUND_REQUESTING, rp.refund_key(case), case["id"]))
    db.commit()
    return None, rp.case_by_uuid(db, case_uuid)


def _check_payment(case, pay):
    """Why Razorpay's payment is not this case's fee, or None."""
    amount = int(case["fee_amount_minor"])
    if (pay.get("id") or "") != case["razorpay_payment_id"]:
        return "provider returned a different payment"
    if (pay.get("order_id") or "") != (case.get("razorpay_order_id") or ""):
        return "payment is not on this case's Razorpay order"
    if pay.get("status") not in ("captured", "refunded"):
        return "payment status is %s, not captured" % pay.get("status")
    if int(pay.get("amount") or 0) != amount:
        return "payment amount %s is not %s" % (pay.get("amount"), amount)
    if (pay.get("currency") or "").upper() != case["fee_currency"]:
        return "payment currency %s is not %s" % (pay.get("currency"), case["fee_currency"])
    return None


def _check_refund(case, ent, key):
    """Why the refund entity is not exactly this case's refund, or None."""
    if (ent.get("payment_id") or "") != case["razorpay_payment_id"]:
        return "refund is for another payment"
    if int(ent.get("amount") or 0) != int(case["fee_amount_minor"]):
        return "refund amount %s is not %s" % (ent.get("amount"), case["fee_amount_minor"])
    if (ent.get("notes") or {}).get("idempotency_key") != key:
        return "refund does not carry this case's key"
    return None


def _event_data(case, **kw):
    data = {"payment_id": case.get("razorpay_payment_id"),
            "amount_minor": int(case["fee_amount_minor"]), "currency": case["fee_currency"],
            "attempts": int(case.get("fee_refund_attempts") or 0)}
    data.update(kw)
    return data


def _exception_locked(db, case, reason):
    cur = db.cursor()
    cur.execute("UPDATE reverse_pickup_cases SET fee_refund_state=%s, fee_refund_error=%s "
                "WHERE id=%s", (rp.REFUND_EXCEPTION, _clip(reason), case["id"]))
    data = _event_data(case, reason="exception", error=_clip(reason))
    rp._audit(db, rp.EV_FEE_REFUND_FAILED, case, data, suffix="exception")
    rp.queue_ops_event(db, rp.EV_FEE_REFUND_FAILED, case["order_id"], data, case=case,
                       pickup=rp.latest_for_order(db, case["order_id"]), key=case["case_uuid"],
                       suffix="exception", commit=False)
    add_history(cur, case["order_id"], "Reverse-pickup fee refund EXCEPTION: %s; refund by hand "
                "only after checking Razorpay" % _clip(reason, 200), case.get("site_from"))
    db.commit()


def _finish(db, case_uuid, outcome, ent=None, error=None, logger=None):
    case = rp.case_by_uuid_for_update(db, case_uuid)
    if case["fee_state"] == rp.FEE_REFUNDED:
        db.rollback()
        return DUPLICATE, case
    cur = db.cursor()
    if outcome == REFUNDED:
        cur.execute("UPDATE reverse_pickup_cases SET fee_state=%s, fee_refunded_minor=%s, "
                    "fee_refund_id=%s, fee_refund_state=%s, fee_refund_error=NULL, "
                    "fee_refunded_at=NOW() WHERE id=%s",
                    (rp.FEE_REFUNDED, int(ent["amount"]), _clip(ent["id"], 64), rp.REFUND_DONE,
                     case["id"]))
        case = rp.case_by_uuid(db, case_uuid)
        data = _event_data(case, refund_id=case["fee_refund_id"], status=ent.get("status"))
        rp._audit(db, rp.EV_FEE_REFUNDED, case, data)
        rp.queue_ops_event(db, rp.EV_FEE_REFUNDED, case["order_id"], data, case=case,
                           pickup=rp.latest_for_order(db, case["order_id"]),
                           key=case["case_uuid"], commit=False)
        rp.enqueue_notice(db, case, rp.NOTICE_FEE_REFUNDED)
        add_history(cur, case["order_id"], "Reverse-pickup fee refunded: INR %d on payment %s "
                    "(refund %s)" % (int(ent["amount"]) // 100, case["razorpay_payment_id"],
                                     case["fee_refund_id"]), case.get("site_from"))
        db.commit()
        if logger:
            logger.info("REVERSE_PICKUP_FEE_REFUNDED case:%s refund:%s"
                        % (case_uuid, case["fee_refund_id"]))
        return REFUNDED, case
    if outcome == EXCEPTION:
        _exception_locked(db, case, error)
        if logger:
            logger.error("REVERSE_PICKUP_FEE_REFUND_EXCEPTION case:%s %s" % (case_uuid, error))
        return EXCEPTION, rp.case_by_uuid(db, case_uuid)
    cur.execute("UPDATE reverse_pickup_cases SET fee_refund_state=%s, fee_refund_error=%s "
                "WHERE id=%s", (rp.REFUND_FAILED, _clip(error), case["id"]))
    if int(case.get("fee_refund_attempts") or 0) <= 1:
        # Ops hears about the first failure; later retries are in the report.
        data = _event_data(case, reason="provider_failed", error=_clip(error))
        rp._audit(db, rp.EV_FEE_REFUND_FAILED, case, data)
        rp.queue_ops_event(db, rp.EV_FEE_REFUND_FAILED, case["order_id"], data, case=case,
                           pickup=rp.latest_for_order(db, case["order_id"]),
                           key=case["case_uuid"], commit=False)
    db.commit()
    if logger:
        logger.warning("REVERSE_PICKUP_FEE_REFUND_FAILED case:%s attempt:%s %s"
                       % (case_uuid, case.get("fee_refund_attempts"), error))
    return FAILED, rp.case_by_uuid(db, case_uuid)


def refund(db, case_uuid, provider, source, environ=None, logger=None):
    """One attempt at the case's fee refund. ``provider`` has ``payment(id)``,
    ``existing_refund(id, key)`` and ``refund(id, amount, key, notes)``.
    Returns ``{"outcome", "case"}``."""
    if not enabled(environ):
        return {"outcome": DISABLED, "case": rp.case_by_uuid(db, case_uuid)}
    rp.ensure_schema(db)
    outcome, case = _claim(db, case_uuid)
    if outcome:
        return {"outcome": outcome, "case": case}
    pid = case["razorpay_payment_id"]
    key = case["fee_refund_key"]
    try:
        pay = provider.payment(pid)
        bad = _check_payment(case, pay)
        if bad:
            outcome, case = _finish(db, case_uuid, EXCEPTION, error=bad, logger=logger)
            return {"outcome": outcome, "case": case}
        if int(pay.get("amount_refunded") or 0):
            ent = provider.existing_refund(pid, key)
            if not ent:
                outcome, case = _finish(db, case_uuid, EXCEPTION, logger=logger,
                                        error="payment already refunded outside this case's refund")
                return {"outcome": outcome, "case": case}
        else:
            ent = provider.refund(pid, int(case["fee_amount_minor"]), key,
                                  notes={"idempotency_key": key, "purpose": PURPOSE,
                                         "case_uuid": case_uuid, "order_id": case["order_id"],
                                         "source": source})
    except Exception as exc:  # noqa: BLE001 - provider down or refused: retried
        outcome, case = _finish(db, case_uuid, FAILED, error=str(exc), logger=logger)
        return {"outcome": outcome, "case": case}
    bad = _check_refund(case, ent or {}, key)
    if bad:
        outcome, case = _finish(db, case_uuid, EXCEPTION, error=bad, logger=logger)
    elif ent.get("status") == "failed":
        outcome, case = _finish(db, case_uuid, FAILED, error="provider refund status failed",
                                logger=logger)
    else:
        outcome, case = _finish(db, case_uuid, REFUNDED, ent=ent, logger=logger)
    return {"outcome": outcome, "case": case}


def retry_pending(db, provider_factory, source="reconcile", environ=None, limit=50, logger=None):
    """Every refund owed and due: PENDING, FAILED at least RETRY_MINUTES ago,
    or an interrupted REQUESTING. A confirmed defect on a PAID fee with no
    refund state (inspected before this code) is owed too."""
    env = os.environ if environ is None else environ
    out = {"due": 0, REFUNDED: 0, FAILED: 0, EXCEPTION: 0, BUSY: 0, DUPLICATE: 0, NOT_ELIGIBLE: 0}
    rp.ensure_schema(db)
    cur = db.cursor()
    cur.execute("UPDATE reverse_pickup_cases SET fee_refund_state=%s, "
                "fee_refund_key=CONCAT('rpfee-refund:', case_uuid) WHERE inspection_defect=1 "
                "AND inspected_at IS NOT NULL AND fee_state=%s AND fee_refund_state IS NULL",
                (rp.REFUND_PENDING, rp.FEE_PAID))
    db.commit()
    if not enabled(env):
        cur.execute("SELECT COUNT(*) AS n FROM reverse_pickup_cases WHERE fee_refund_state IN "
                    "(%s,%s,%s)", rp.REFUND_OPEN)
        return dict(out, off=True, open=int(cur.fetchone()["n"]))
    minutes = int(env.get(RETRY_MINUTES_ENV) or RETRY_MINUTES)
    cur.execute("SELECT case_uuid FROM reverse_pickup_cases WHERE fee_refund_state=%s "
                "OR (fee_refund_state=%s AND (fee_refund_last_attempt_at IS NULL OR "
                "fee_refund_last_attempt_at <= NOW() - INTERVAL %s MINUTE)) "
                "OR (fee_refund_state=%s AND fee_refund_last_attempt_at <= NOW() - INTERVAL %s MINUTE) "
                "ORDER BY id LIMIT %s",
                (rp.REFUND_PENDING, rp.REFUND_FAILED, minutes, rp.REFUND_REQUESTING,
                 STALE_MINUTES, int(limit)))
    uuids = [r["case_uuid"] for r in cur.fetchall()]
    db.commit()
    if not uuids:
        return out
    provider = provider_factory()
    for case_uuid in uuids:
        out["due"] += 1
        res = refund(db, case_uuid, provider, source, environ=env, logger=logger)
        out[res["outcome"]] = out.get(res["outcome"], 0) + 1
    return out
