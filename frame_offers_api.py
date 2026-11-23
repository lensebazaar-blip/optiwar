"""Ops frame-offer API and the one-time customer selection page."""

import html
import hmac
import secrets
import time
from decimal import Decimal
from threading import Lock

from flask import (current_app, jsonify, make_response, redirect, render_template,
                   request, session, url_for)

from . import frame_offers as offers
from .db import get_db


SESSION_KEY = "frame_offer_guest"
_limit_lock = Lock()
_hits = {}


def _db():
    db = get_db()
    offers.ensure_schema(db)
    return db


def _no_referrer(response):
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _page(state, status=200, **context):
    response = make_response(render_template("frame_offer_guest.html",
                                             state=state, **context))
    response.status_code = status
    return _no_referrer(response)


def _allow(ip):
    now = time.time()
    with _limit_lock:
        hits = [at for at in _hits.get(ip, []) if now - at < 60]
        if len(hits) >= 20:
            _hits[ip] = hits
            return False
        hits.append(now)
        _hits[ip] = hits
    return True


def _client_ip():
    return request.remote_addr or "unknown"


def _guest_csrf_ok():
    expected = (session.get(SESSION_KEY) or {}).get("csrf") or ""
    supplied = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
    return bool(expected) and hmac.compare_digest(expected, supplied)


def _api_error(exc):
    return jsonify({"ok": False, "error": exc.code, "message": str(exc)}), exc.status


def _ops_auth():
    from .order_face_scan import _ops_auth as require_ops_auth
    return require_ops_auth()


def _decision_notice(result):
    admin = current_app.config.get("ADMIN_NOTIFY_EMAIL", "admin@optiwar.com")
    if result["action"] == "refund":
        choice, action = "refund", "full refund request"
    else:
        choice = result.get("product_code") or "replacement"
        amount = Decimal(int(result.get("amount_minor") or 0)) / 100
        amount_text = format(amount, ",.2f").rstrip("0").rstrip(".")
        settlement = result.get("settlement")
        if settlement == "WAIVED":
            action = "none"
        elif settlement == "REFUND_DUE":
            action = "refund ₹%s" % amount_text
        else:
            action = "collect ₹%s" % amount_text
    summary = "customer chose %s — action: %s" % (choice, action)
    try:
        from . import notifications
        notifications.send_email(
            admin, "OW-%s: %s" % (result["order_id"], summary),
            "<p>%s</p>" % html.escape(summary), summary)
    except Exception as exc:
        current_app.logger.warning("Frame offer admin notice failed: %s", exc)


def register(bp):
    @bp.route("/ops/api/orders/<order_id>/frame-matches", methods=["GET"])
    def frame_offer_matches(order_id):
        if not _ops_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        db = _db()
        cur = db.cursor()
        line_id = request.args.get("order_line_id")
        if not line_id:
            return jsonify({"ok": False, "error": "order_line_id_required"}), 400
        line = offers._line(cur, order_id, line_id)
        if not line:
            return jsonify({"ok": False, "error": "line_not_found"}), 404
        rows = offers.matches(cur, line["product_id"], line.get("site_from"))
        return jsonify({"ok": True, "order_line_id": line["order_line_id"],
                        "failed_price_minor": offers._minor(
                            line.get("product_special_price")),
                        "failed_product": {"product_id": line["product_id"],
                                           "code": line.get("product_code") or "",
                                           "name": line.get("product_name") or "",
                                           "size": line.get("product_size") or "",
                                           "colour": line.get("color_display") or ""},
                        "matches": offers._candidate_rows(cur, rows)})

    @bp.route("/ops/api/orders/<order_id>/frame-offer", methods=["POST"])
    def frame_offer_create(order_id):
        if not _ops_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        db = _db()
        try:
            row, _token, link = offers.create_offer(
                db, order_id, data.get("order_line_id"), data.get("reason"),
                data.get("product_ids"), data.get("created_by"),
                token_secret=current_app.secret_key)
        except offers.OfferError as exc:
            return _api_error(exc)
        current_app.logger.info("FRAME_OFFER:CREATED offer=%s order=%s line=%s",
                                row["offer_uuid"], order_id, row["order_line_id"])
        return jsonify({"ok": True, "offer_uuid": row["offer_uuid"], "link": link,
                        "expires_at": row["expires_at"],
                        "candidates": row["candidates"]}), 201

    @bp.route("/ops/api/orders/<order_id>/frame-offer", methods=["GET"])
    def frame_offer_latest(order_id):
        if not _ops_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        db = _db()
        line_id = request.args.get("order_line_id") or None
        rows = offers.latest_for_order(db, order_id, line_id)
        return jsonify({"ok": True, "offers": [offers.public_view(r) for r in rows]})

    @bp.route("/ops/api/orders/<order_id>/frame-offer/<offer_uuid>/settled",
               methods=["POST"])
    def frame_offer_settled(order_id, offer_uuid):
        if not _ops_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        db = _db()
        row = offers.get_by_uuid(db, offer_uuid)
        if not row or str(row["order_id"]) != str(order_id):
            return jsonify({"ok": False, "error": "not_found"}), 404
        changed = offers.mark_settled(db, offer_uuid, data.get("ref"))
        return jsonify({"ok": changed})

    @bp.route("/ops/api/frame-offers/open", methods=["GET"])
    def frame_offers_open():
        if not _ops_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return jsonify({"ok": True, "offers": offers.open_offers(_db())})

    @bp.route("/ops/api/frame-offers/<offer_uuid>/link", methods=["GET"])
    def frame_offer_reminder_link(offer_uuid):
        if not _ops_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        row = offers.get_by_uuid(_db(), offer_uuid)
        if not row or row.get("status") != "OFFERED":
            return jsonify({"ok": False, "error": "not_found"}), 404
        cur = get_db().cursor()
        cur.execute("SELECT site_from FROM orders WHERE order_line_id=%s",
                    (row["order_line_id"],))
        line = cur.fetchone() or {}
        row["site_from"] = line.get("site_from")
        try:
            return jsonify({"ok": True, "link": offers.link_for(
                row, current_app.secret_key)})
        except offers.OfferError as exc:
            return _api_error(exc)

    @bp.route("/ops/api/frame-offers/<offer_uuid>/mark", methods=["POST"])
    def frame_offer_mark(offer_uuid):
        if not _ops_auth():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        try:
            changed = offers.mark(_db(), offer_uuid, data.get("action"))
        except offers.OfferError as exc:
            return _api_error(exc)
        return jsonify({"ok": changed})

    @bp.route("/fq/<token>", methods=["GET"])
    def frame_offer_guest(token):
        if not _allow(_client_ip()):
            return _page("invalid", 429)
        db = _db()
        row = offers.by_token(db, token)
        if not row:
            return _page("invalid", 404)
        if row["status"] != "OFFERED" or row["expires_at"] <= offers.datetime.now():
            return _page("expired", 410)
        row = offers.mark_opened(db, row)
        cur = db.cursor()
        cur.execute("SELECT order_total FROM orders WHERE order_line_id=%s",
                    (row["order_line_id"],))
        total = cur.fetchone() or {}
        row["order_total_minor"] = offers._minor(total.get("order_total"))
        session[SESSION_KEY] = {"uuid": row["offer_uuid"],
                                "hash": row["token_hash"],
                                "csrf": secrets.token_urlsafe(32)}
        for candidate in row["candidates"]:
            diff = int(candidate["price_minor"]) - int(row["failed_price_minor"])
            candidate["price_label"] = (
                "No extra charge" if abs(diff) < 5000 or diff < 0 else
                "Pay ₹%s more" % (diff // 100))
        return _page("choose", offer=row, csrf=session[SESSION_KEY]["csrf"])

    @bp.route("/fq/choose", methods=["POST"])
    def frame_offer_choose():
        if not _allow(_client_ip()):
            return _page("invalid", 429)
        bound = session.get(SESSION_KEY) or {}
        if not _guest_csrf_ok():
            return _page("invalid", 403)
        db = _db()
        row = offers.get_by_uuid(db, bound.get("uuid"))
        if not row or row["token_hash"] != bound.get("hash"):
            return _page("expired", 410)
        if row["status"] != "OFFERED":
            return _page("expired", 410)
        refund = request.form.get("refund") == "1"
        try:
            result = offers.decide(
                db, row, product_id=request.form.get("product_id"),
                refund=refund)
        except offers.OfferError as exc:
            if exc.code == "sold_out":
                for candidate in row["candidates"]:
                    diff = int(candidate["price_minor"]) - int(row["failed_price_minor"])
                    candidate["price_label"] = (
                        "No extra charge" if abs(diff) < 5000 or diff < 0 else
                        "Pay ₹%s more" % (diff // 100))
                return _page("choose", 409, offer=row, csrf=bound["csrf"],
                             error="That frame just sold out. Please choose another.")
            return _page("expired", exc.status)
        session.pop(SESSION_KEY, None)
        session["frame_offer_done"] = result["action"]
        _decision_notice(result)
        return _no_referrer(redirect(url_for("main.frame_offer_guest_done"), 303))

    @bp.route("/fq/done", methods=["GET"])
    def frame_offer_guest_done():
        action = session.pop("frame_offer_done", "")
        message = ("Your full refund request has been sent to the Optiwar team for review."
                   if action == "refund" else
                   "Your replacement choice has been recorded. The Optiwar team will fit and dispatch your frame.")
        return _page("done", message=message)
