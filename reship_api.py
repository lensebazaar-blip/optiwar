"""The reship routes — customer, Ops, and the webhook branch.

Customer (signed-in owner of the order, India site only; anything else 404):

    GET  /api/orders/<order_id>/reship                  state the card shows
    POST /api/orders/<order_id>/reship                  same, explicit ask
    POST /api/reshipments/<uuid>/payment/create         the one ₹250 Razorpay order
    POST /api/reshipments/<uuid>/payment/verify         browser callback

Ops (``ops._require_ops_auth``: admin session or Bearer OPS_API_TOKEN):

    GET  /ops/reship                                    the queue page
    GET  /ops/api/reship/queue
    POST /ops/api/shipments/<order_id>/return-received  Confirm Returned to Ops
    POST /ops/api/reshipments/<uuid>/ship               record the new AWB

Routes attach to the main blueprint; ``__init__.py`` and ``ops.py`` are not in
the deployment set.
"""
from flask import current_app, jsonify, render_template, request, session

from . import reship
from .db import get_db
from .notifications import notify_order_shipped
from .payments import (create_reship_razorpay_order, fetch_razorpay_payment,
                       verify_razorpay_payment)


def _error(exc):
    if exc.status == 404:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": False, "error": exc.code, "message": str(exc)}), exc.status


def _customer():
    return session.get("user_id")


def _body():
    return request.get_json(silent=True) or {}


def _ops_operator(body=None):
    """Who acted: the admin session, else the platform's own operator name
    (``operator`` in the body) recorded under the token so the audit names
    the person, not only the integration."""
    email = session.get("user_email")
    if email:
        return email
    who = str((body or {}).get("operator") or "").strip()[:80]
    return "ops-api-token:%s" % who if who else "ops-api-token"


def _ops_auth():
    from .ops import _require_ops_auth
    return _require_ops_auth()


def _notify(db, event, row, host):
    """Business state is committed before this runs; a failure here is logged
    by the claim and changes nothing."""
    try:
        return reship.notify(db, event, row["order_id"], row.get("customer_id"), host,
                             reship_uuid=row["reship_uuid"])
    except Exception as exc:  # noqa: BLE001
        current_app.logger.error("RESHIP_NOTIFY_ERROR event:%s reship:%s %s"
                                 % (event, row["reship_uuid"], exc))
        return {"sent": False}


def settle_and_notify(db, reship_uuid, payment, source, host):
    res = reship.settle_payment(db, reship_uuid, payment, source, logger=current_app.logger)
    if res["outcome"] == reship.APPLIED:
        _notify(db, reship.EV_PAYMENT_COMPLETED, res["row"], host)
    return res


def register(bp):

    # ------------------------------------------------------------ customer

    @bp.route("/api/orders/<order_id>/reship", methods=["GET", "POST"])
    def reship_state(order_id):
        if not _customer():
            return jsonify({"ok": False, "error": "login_required"}), 401
        db = get_db()
        try:
            head, row = reship.customer_reship(db, _customer(), order_id, request.host)
        except reship.ReshipError as exc:
            return _error(exc)
        cur = db.cursor()
        latest = reship._latest_status(cur, order_id)
        view = reship.public_view(row, latest, shipment=reship.original_shipment(cur, order_id))
        return jsonify({"ok": True, "order_id": order_id, "reship": view})

    @bp.route("/api/reshipments/<reship_uuid>/payment/create", methods=["POST"])
    def reship_payment_create(reship_uuid):
        """The browser sends the identifier and nothing else. Amount,
        currency, notes and receipt are the server's."""
        if not _customer():
            return jsonify({"ok": False, "error": "login_required"}), 401
        db = get_db()

        def create_order(amount, currency, receipt, notes):
            return create_reship_razorpay_order(amount, currency, receipt, notes)

        try:
            row, created = reship.begin_payment(db, _customer(), reship_uuid, request.host,
                                                create_order, logger=current_app.logger)
        except reship.ReshipError as exc:
            return _error(exc)
        except Exception as exc:  # noqa: BLE001 - provider down
            current_app.logger.error("RESHIP_RZP_ORDER_FAILED reship:%s %s" % (reship_uuid, exc))
            return jsonify({"ok": False, "error": "provider_unavailable",
                            "message": "Payment could not be started; please retry"}), 502
        return jsonify({"ok": True, "created": created,
                        "razorpay_order_id": row["razorpay_order_id"],
                        "amount": int(row["fee_minor"]), "currency": row["fee_currency"],
                        "key_id": current_app.config.get("RAZORPAY_KEY_ID", ""),
                        "order_id": row["order_id"]})

    @bp.route("/api/reshipments/<reship_uuid>/payment/verify", methods=["POST"])
    def reship_payment_verify(reship_uuid):
        """Browser callback. The signature proves Razorpay signed these ids;
        the payment itself is then fetched server-side and settled by
        ``reship.settle_payment`` — the browser's amount or state is never read."""
        if not _customer():
            return jsonify({"ok": False, "error": "login_required"}), 401
        db = get_db()
        row = reship.by_uuid(db, reship_uuid)
        if (not row or int(row.get("customer_id") or 0) != int(_customer())
                or not reship.workflow_open(request.host, row.get("site_from"), row["order_id"])):
            return jsonify({"ok": False, "error": "not_found"}), 404
        body = _body()
        pid = (body.get("razorpay_payment_id") or "").strip()
        oid = (body.get("razorpay_order_id") or "").strip()
        sig = (body.get("razorpay_signature") or "").strip()
        if not (pid and oid and sig) or oid != (row["razorpay_order_id"] or ""):
            return jsonify({"ok": False, "error": "verification_failed"}), 400
        if not verify_razorpay_payment(oid, pid, sig):
            current_app.logger.warning("RESHIP_VERIFY_BAD_SIGNATURE reship:%s payment:%s"
                                       % (reship_uuid, pid))
            return jsonify({"ok": False, "error": "verification_failed"}), 400
        payment = fetch_razorpay_payment(pid)
        if not payment or (payment.get("order_id") or "") != oid:
            return jsonify({"ok": False, "error": "verification_failed"}), 400
        res = settle_and_notify(db, reship_uuid, payment, "browser_callback", request.host)
        if res["outcome"] in (reship.APPLIED, reship.DUPLICATE):
            latest = reship._latest_status(db.cursor(), row["order_id"])
            return jsonify({"ok": True, "reship": reship.public_view(
                reship.by_uuid(db, reship_uuid), latest)})
        if res["outcome"] == reship.NOT_CAPTURED:
            return jsonify({"ok": False, "error": "not_captured",
                            "message": "Payment not captured yet"}), 202
        return jsonify({"ok": False, "error": "verification_failed"}), 400

    # ---------------------------------------------------------------- ops

    @bp.route("/ops/reship", methods=["GET"])
    def ops_reship_page():
        if not _ops_auth():
            return jsonify({"error": "Unauthorized"}), 401
        q = reship.ops_queue(get_db())
        return render_template("ops_reship.html", queue=q, fee=reship.FEE_INR,
                               enabled=reship.enabled())

    @bp.route("/ops/api/reship/queue", methods=["GET"])
    def ops_reship_queue():
        if not _ops_auth():
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"ok": True, **reship.ops_queue(get_db())})

    @bp.route("/ops/api/shipments/<order_id>/return-received", methods=["POST"])
    def ops_return_received(order_id):
        """Confirm Returned to Ops: the parcel is physically in hand."""
        if not _ops_auth():
            return jsonify({"error": "Unauthorized"}), 401
        body = _body()
        db = get_db()
        before = reship.active_for_order(db, order_id)
        try:
            row = reship.confirm_returned(db, order_id, _ops_operator(body),
                                          original_awb=body.get("original_awb"),
                                          courier=body.get("courier"),
                                          return_reason=body.get("return_reason"))
        except reship.ReshipError as exc:
            return _error(exc)
        created = before is None
        if created:
            host = row.get("site_from") or request.host
            _notify(db, reship.EV_AVAILABLE, row, host)
        return jsonify({"ok": True, "created": created, "reship": _ops_row(row)})

    @bp.route("/ops/api/reshipments/<reship_uuid>/ship", methods=["POST"])
    def ops_reship_ship(reship_uuid):
        if not _ops_auth():
            return jsonify({"error": "Unauthorized"}), 401
        body = _body()
        db = get_db()
        before = reship.by_uuid(db, reship_uuid)
        try:
            row = reship.ship(db, reship_uuid, _ops_operator(body), body.get("new_awb"),
                              body.get("new_courier"))
        except reship.ReshipError as exc:
            return _error(exc)
        if before and before["status"] != reship.ST_RESHIPPED:
            host = row.get("site_from") or request.host
            try:
                reship.notify_shipped(db, row, host, notify_order_shipped)
            except Exception as exc:  # noqa: BLE001
                current_app.logger.error("RESHIP_NOTIFY_ERROR event:shipped reship:%s %s"
                                         % (reship_uuid, exc))
        return jsonify({"ok": True, "reship": _ops_row(row)})


def _ops_row(row):
    keys = ("reship_uuid", "order_id", "customer_id", "status", "payment_status",
            "fee_amount", "fee_currency", "original_awb", "original_courier",
            "return_reason", "razorpay_order_id", "razorpay_payment_id",
            "ops_return_confirmed_by", "ops_return_confirmed_at", "paid_at",
            "reshipped_at", "new_awb", "new_courier", "shipped_by")
    return {k: row.get(k) for k in keys}
