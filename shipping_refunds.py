"""Possible refunds: paid delivery options that missed their promise (owner, 9 Nov 2026).

A customer who paid for Expedite or 24-hour delivery is owed that fee back when
the order was not delivered by ``order_shipping.promised_date``. Nothing here
refunds on its own:

* **The list** is every paid order with a delivery fee whose promise has
  passed and that was delivered late or is not delivered yet. Delivery and
  dispatch dates are read from the tracking the site already keeps: the first
  ``Complete`` (delivered) and first ``Shipped`` rows in ``order_status``.
* **The indicator** is the count of rows not yet refunded; the Ops page blinks
  it and the JSON endpoint gives the same count to any other dashboard.
* **The refund is a person's click.** It goes through ``refunds.execute``, the
  engine the Ops refund API uses: validated against the captured Razorpay
  payment, refunded to source, idempotent, and written to ``order_refunds``
  with who asked and when. The amount is the stored fee, never a typed one.

Routes (``ops._require_ops_auth``: admin session or Bearer ``OPS_API_TOKEN``):

    GET  /ops/possible-refunds                                   the page
    GET  /api/ops/shipping/possible-refunds                      the list + count
    POST /api/ops/shipping/possible-refunds/<order_id>/refund    refund the fee

Stdlib at import; Flask, the DB, the refund engine and the provider are
imported inside the functions that need them.
"""
import datetime

KEY_PREFIX = "shipfee-"
REASON_CODE = "GOODWILL"            # an existing refunds.REASON_CODES member; the comment says why
SERVICE_IDENTITY = "ops-console"
CURRENCY = "INR"                    # delivery options are sold on optiwar.in only
DONE = ("PROCESSED", "PENDING", "CREATED")   # a refund that happened or is in flight

CANDIDATES_SQL = (
    "SELECT s.order_id, s.option_key, s.fee, s.promised_date, s.state, s.zipcode, "
    "(SELECT MIN(os.created_at) FROM order_status os WHERE os.order_id = s.order_id "
    "  AND os.order_status_name = 'Complete') AS delivered_at, "
    "(SELECT MIN(os.created_at) FROM order_status os WHERE os.order_id = s.order_id "
    "  AND os.order_status_name = 'Shipped') AS shipped_at, "
    "(SELECT os.order_status_name FROM order_status os WHERE os.order_id = s.order_id "
    "  ORDER BY os.order_status_id DESC LIMIT 1) AS latest_status, "
    "pc.payment_date "
    "FROM order_shipping s "
    "JOIN (SELECT order_id, MIN(date_created) AS payment_date FROM payment_collector "
    "      WHERE status = 'TXN_SUCCESS' AND payment_dump NOT LIKE '%%TEST_BUY%%' "
    "      GROUP BY order_id) pc ON pc.order_id = s.order_id "
    "WHERE s.fee > 0 AND s.promised_date < %s ORDER BY s.promised_date, s.order_id"
)

OPTION_LABELS = {"expedite": "Expedite", "ncr24": "24-hour delivery (Delhi NCR)"}


def _date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    return value


def lateness(row, today):
    """``(days_late, basis)`` for a paid delivery option, or ``(0, "")`` when the
    promise was kept or has not passed. ``basis`` says which fact made it late."""
    promised = _date(row.get("promised_date"))
    if not promised:
        return 0, ""
    delivered = _date(row.get("delivered_at"))
    if delivered:
        days = (delivered - promised).days
        return (days, "delivered late") if days > 0 else (0, "")
    days = (today - promised).days
    return (days, "not delivered yet") if days > 0 else (0, "")


def refund_rows(cursor, order_ids):
    """``{order_id: [ledger rows]}`` for the fee refunds already asked for."""
    ids = [str(i) for i in order_ids if i]
    if not ids:
        return {}
    try:
        cursor.execute(
            "SELECT order_id, idempotency_key, status, amount_minor, requested_by, requested_at, "
            "completed_at, provider_refund_id, error_text FROM order_refunds "
            "WHERE idempotency_key LIKE %%s AND order_id IN (%s) ORDER BY refund_id" % ",".join(["%s"] * len(ids)),
            [KEY_PREFIX + "%"] + ids)
        out = {}
        for r in cursor.fetchall():
            out.setdefault(str(r["order_id"]), []).append(r)
        return out
    except Exception:  # noqa: BLE001 - no ledger yet means nothing was refunded
        return {}


def refund_state(ledger):
    """The row that settles the question for an order: the refund that happened
    (or is in flight) if any, else the last failed attempt, else None."""
    done = [r for r in ledger or [] if r.get("status") in DONE]
    if done:
        return done[-1]
    return (ledger or [None])[-1]


def next_key(order_id, ledger):
    """The idempotency key for the next attempt: the plain key the first time, a
    numbered one after a failed attempt (a failed key can never be replayed)."""
    n = len(ledger or [])
    return "%s%s" % (KEY_PREFIX, order_id) + ("-r%d" % (n + 1) if n else "")


def candidates(cursor, today=None):
    """Every paid delivery option that missed its promise, oldest promise first,
    each with its refund state. [] when the tables are not there."""
    today = today or datetime.date.today()
    try:
        cursor.execute(CANDIDATES_SQL, (today,))
        rows = [dict(r) for r in cursor.fetchall()]
    except Exception:  # noqa: BLE001
        return []
    out = []
    for row in rows:
        days, basis = lateness(row, today)
        if not days:
            continue
        row["days_late"], row["basis"] = days, basis
        row["option_label"] = OPTION_LABELS.get(row.get("option_key"), row.get("option_key") or "")
        row["fee_minor"] = int(round(float(row.get("fee") or 0) * 100))
        out.append(row)
    ledgers = refund_rows(cursor, [r["order_id"] for r in out])
    for row in out:
        ledger = ledgers.get(str(row["order_id"]), [])
        state = refund_state(ledger)
        row["ledger"] = ledger
        row["refund"] = state
        row["refunded"] = bool(state and state.get("status") in DONE)
    return out


def open_count(rows):
    return sum(1 for r in rows if not r.get("refunded"))


def as_json(row):
    def iso(value):
        return value.isoformat() if hasattr(value, "isoformat") else value
    refund = row.get("refund") or {}
    return {
        "order_id": row["order_id"], "option": row.get("option_key"), "option_label": row.get("option_label"),
        "fee": float(row.get("fee") or 0), "fee_minor": row.get("fee_minor"), "currency": CURRENCY,
        "promised_date": iso(row.get("promised_date")), "shipped_at": iso(row.get("shipped_at")),
        "delivered_at": iso(row.get("delivered_at")), "latest_status": row.get("latest_status"),
        "days_late": row.get("days_late"), "basis": row.get("basis"), "refunded": bool(row.get("refunded")),
        "refund": ({"status": refund.get("status"), "requested_by": refund.get("requested_by"),
                    "requested_at": iso(refund.get("requested_at")), "completed_at": iso(refund.get("completed_at")),
                    "provider_refund_id": refund.get("provider_refund_id"), "error": refund.get("error_text")}
                   if refund else None),
    }


def comment_for(row):
    promised = row.get("promised_date")
    promised = promised.strftime("%d %b %Y") if hasattr(promised, "strftime") else str(promised)
    return ("Delivery promise missed: %s fee Rs %d, promised by %s, %s (%d day%s late). Fee refunded to source by Ops."
            % (row.get("option_label") or row.get("option_key"), int(float(row.get("fee") or 0)), promised,
               row.get("basis"), row.get("days_late"), "" if row.get("days_late") == 1 else "s"))


def register(bp):
    from flask import current_app, jsonify, render_template, request, session  # noqa: PLC0415

    def _auth():
        from .ops import _require_ops_auth  # noqa: PLC0415
        return _require_ops_auth()

    def _operator():
        """The person clicking: the admin session's mailbox, or the name a dashboard sends."""
        who = (session.get("user_email") or request.headers.get("X-Ops-Operator") or "").strip()[:191]
        return who or None

    def _rows():
        from .db import get_db  # noqa: PLC0415
        return candidates(get_db().cursor())

    @bp.route("/api/ops/shipping/possible-refunds", methods=["GET"])
    def ops_shipping_possible_refunds():
        if not _auth():
            return jsonify({"error": "Unauthorized"}), 401
        rows = _rows()
        return jsonify({"ok": True, "open": open_count(rows), "count": len(rows),
                        "rows": [as_json(r) for r in rows]})

    @bp.route("/ops/possible-refunds", methods=["GET"])
    def ops_possible_refunds_page():
        if not _auth():
            return jsonify({"error": "Unauthorized"}), 401
        rows = _rows()
        resp = current_app.make_response(render_template(
            "ops_possible_refunds.html", rows=rows, open_count=open_count(rows), operator=_operator() or ""))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @bp.route("/api/ops/shipping/possible-refunds/<order_id>/refund", methods=["POST"])
    def ops_shipping_refund_fee(order_id):
        if not _auth():
            return jsonify({"error": "Unauthorized"}), 401
        operator = _operator()
        if not operator:
            return jsonify({"ok": False, "error": "operator_required",
                            "message": "Name the person approving this refund (X-Ops-Operator)."}), 400
        from .db import get_db  # noqa: PLC0415
        from . import ops_refunds, refunds  # noqa: PLC0415
        db = get_db()
        cursor = db.cursor()
        row = next((r for r in candidates(cursor) if str(r["order_id"]) == str(order_id)), None)
        if row is None:
            return jsonify({"ok": False, "error": "not_a_candidate",
                            "message": "This order has no paid delivery option that missed its promise."}), 404
        if row["refunded"]:
            return jsonify({"ok": False, "error": "already_refunded", "refund": as_json(row)["refund"],
                            "message": "The delivery fee for this order has already been refunded."}), 409
        refunds.ensure_schema(cursor)
        try:
            ledger = refunds.execute(
                db, str(order_id), amount_minor=row["fee_minor"], currency=CURRENCY,
                reason_code=REASON_CODE, comment=comment_for(row),
                idempotency_key=next_key(order_id, row["ledger"]), requested_by=operator,
                service_identity=SERVICE_IDENTITY, approved_message=None,
                provider=ops_refunds._provider(), requested_status=None, logger=current_app.logger)
        except refunds.RefundRejected as exc:
            current_app.logger.info("ACTIVITY:SHIPPING_FEE_REFUND_REJECTED order:%s reason:%s by:%s",
                                    order_id, exc.code, operator)
            return jsonify({"ok": False, "error": exc.code, "message": exc.message}), exc.http_status
        except ops_refunds.ProviderError as exc:
            return jsonify({"ok": False, "error": "provider_unavailable", "message": str(exc)}), 502
        ok = ledger.get("status") != refunds.FAILED
        current_app.logger.info("ACTIVITY:SHIPPING_FEE_REFUND order:%s fee_minor:%s status:%s by:%s",
                                order_id, row["fee_minor"], ledger.get("status"), operator)
        return jsonify({"ok": ok, "order_id": str(order_id), "amount_minor": row["fee_minor"],
                        "status": ledger.get("status"), "requested_by": operator,
                        "provider_refund_id": ledger.get("provider_refund_id"),
                        "error": ledger.get("error_text")}), (200 if ok else 502)
