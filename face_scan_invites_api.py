"""
Routes for remote face-scan requests.

Owner side (signed in, gated, object-authorised the FACE-A way):

    POST /api/face-profiles/<id>/scan-request           create (replaces active)
    GET  /api/face-profiles/<id>/scan-request           current state
    POST /api/face-profiles/<id>/scan-request/resend    new token, same or new channel
    POST /api/face-profiles/<id>/scan-request/cancel

Guest side (no account, no login, no navigation):

    GET  /face-scan/request/<token>       verify; bind to a session; redirect
    GET  /face-scan/guest                 consent page
    POST /face-scan/guest/consent
    GET  /face-scan/guest/scan            the try-on scanner in guest mode
    POST /face-scan/guest/api/save        token-authorised completion
    GET  /face-scan/guest/done

The bearer token appears in exactly one request — the first GET — and is
then exchanged for a server-side session binding (``request_uuid`` plus the
token's hash), so it is absent from every later URL, referer and access-log
line. Every guest response is ``Cache-Control: no-store`` and
``Referrer-Policy: no-referrer``. Each guest route re-reads the request row
and re-checks status and expiry; a cancellation wins over an open page at
the moment of submission.

The two guest POSTs do not rely on the site-wide Origin/Referer guard: a
page served with ``no-referrer`` sends neither header (Origin arrives as the
literal ``null``), so that guard can only ever refuse them. They are exempt
from it by exact endpoint and carry their own proof instead — a per-guest
``csrf`` secret minted at the first hop, kept in the same server-side
binding, and required back as a hidden form field or ``X-Face-Scan-Csrf``
header. The binding never grants an account session: the rows it reaches
are selected by ``request_uuid`` and token hash, never by ``user_id``.
"""
import base64
import hmac
import json
import os
import secrets

from flask import (abort, current_app, jsonify, redirect, render_template,
                   request, session, url_for)

from . import face_profiles as fp
from . import face_profiles_api as fpa
from . import face_scan_invites as fsi
from .db import get_db

SESSION_KEY = "face_scan_guest"
CSRF_FIELD = "_guest_csrf"
CSRF_HEADER = "X-Face-Scan-Csrf"
_guest_limiter = fsi.IpLimiter(max_hits=30, window_seconds=600)


def gate_enabled(environ=None):
    env = os.environ if environ is None else environ
    merged = dict(env)
    for key in (fpa.ENABLED_ENV, fpa.ALLOW_ENV, fsi.ENABLED_ENV, fsi.ALLOW_ENV):
        if key in current_app.config:
            merged[key] = current_app.config[key]
    return fsi.enabled_for(session.get("user_email"), merged)


def _db():
    db = get_db()
    fsi.ensure_schema(db)
    return db


def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() if xff else request.remote_addr) or ""


def _require():
    if not session.get("user_id"):
        return jsonify({"ok": False, "error": "login_required"}), 401
    if not gate_enabled():
        return jsonify({"ok": False, "error": "not_found"}), 404
    return None


def _error(exc):
    return jsonify({"ok": False, "error": exc.code, "message": str(exc)}), exc.status


def _guest_headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def _site_host():
    return current_app.config.get("FACE_SCAN_LINK_BASE") or request.host


def attach_state(db, customer_id, profiles):
    """Add ``scan_request`` to each My Faces card view."""
    fsi.ensure_schema(db)
    current = fsi.for_customer(db, customer_id)
    for p in profiles:
        p["scan_request"] = fsi.public_view(current.get(int(p["id"])))
    return profiles


def _create_and_send(db, customer_id, profile_id, channel, destination):
    """Returns ``(row, link)``. The link is handed to the sender exactly once,
    in the response to this call, so the request can still be shared when the
    WhatsApp or email never arrives; no later read can rebuild it."""
    row, token = fsi.create(
        db, customer_id, profile_id, channel, destination,
        sender_name=session.get("user_name") or "",
        site_host=_site_host(), created_ip=_client_ip())
    current_app.logger.info("FACE_SCAN_REQUEST:CREATED customer=%s profile=%s "
                            "request=%s channel=%s", customer_id, profile_id,
                            row["request_uuid"], row["channel"])
    try:
        row = fsi.send(db, row, token)
    except Exception as exc:  # noqa: BLE001 - delivery must never break the request
        current_app.logger.warning("FACE_SCAN_REQUEST:SEND_ERROR request=%s err=%s",
                                   row["request_uuid"], type(exc).__name__)
        row = fsi.by_uuid(db, row["request_uuid"])
    return row, fsi.link_for(token, row.get("site_host"))


def _guest_row(db):
    """The request the guest session is bound to, if still usable."""
    bound = session.get(SESSION_KEY) or {}
    if not bound.get("uuid") or not bound.get("hash") or not bound.get("csrf"):
        return None
    row = fsi.by_uuid(db, bound["uuid"])
    if not row or row["token_hash"] != bound["hash"]:
        return None
    return fsi.refresh(db, row)


def _guest_csrf_ok():
    """The secret minted with this guest binding must come back on every
    guest POST — from the form, or from the scanner's fetch header."""
    want = (session.get(SESSION_KEY) or {}).get("csrf") or ""
    got = request.form.get(CSRF_FIELD) or request.headers.get(CSRF_HEADER) or ""
    return bool(want) and hmac.compare_digest(want, got)


def _guest_page(template, status=200, **ctx):
    resp = current_app.make_response(render_template(template, **ctx))
    resp.status_code = status
    return _guest_headers(resp)


def register(bp):

    # ---------------------------------------------------------------- owner

    @bp.route("/api/face-profiles/<int:profile_id>/scan-request", methods=["GET"])
    def face_scan_request_get(profile_id):
        refused = _require()
        if refused:
            return refused
        try:
            row = fsi.for_profile(_db(), session["user_id"], profile_id)
        except fp.ProfileError as exc:
            return _error(exc)
        return jsonify({"ok": True, "scan_request": fsi.public_view(row)})

    @bp.route("/api/face-profiles/<int:profile_id>/scan-request", methods=["POST"])
    def face_scan_request_create(profile_id):
        refused = _require()
        if refused:
            return refused
        data = request.get_json(silent=True) or {}
        try:
            row, link = _create_and_send(_db(), session["user_id"], profile_id,
                                         data.get("channel"), data.get("destination"))
        except fp.ProfileError as exc:
            return _error(exc)
        return jsonify({"ok": True, "scan_request": fsi.public_view(row), "link": link}), 201

    @bp.route("/api/face-profiles/<int:profile_id>/scan-request/resend", methods=["POST"])
    def face_scan_request_resend(profile_id):
        """A new token replacing the old one; the channel and destination
        default to the current request's."""
        refused = _require()
        if refused:
            return refused
        db = _db()
        data = request.get_json(silent=True) or {}
        try:
            cur = fsi.for_profile(db, session["user_id"], profile_id)
            channel = data.get("channel") or (cur["channel"] if cur else None)
            dest = data.get("destination") or (
                (cur.get("recipient_email") or cur.get("recipient_phone")) if cur else None)
            if not channel or not dest:
                raise fsi.InviteError("nothing_to_resend", "Choose how to send the request")
            row, link = _create_and_send(db, session["user_id"], profile_id, channel, dest)
        except fp.ProfileError as exc:
            return _error(exc)
        return jsonify({"ok": True, "scan_request": fsi.public_view(row), "link": link}), 201

    @bp.route("/api/face-profiles/<int:profile_id>/scan-request/cancel", methods=["POST"])
    def face_scan_request_cancel(profile_id):
        refused = _require()
        if refused:
            return refused
        db = _db()
        try:
            cur = fsi.for_profile(db, session["user_id"], profile_id)
            if cur and cur["status"] in fsi.USABLE:
                cur = fsi.cancel(db, session["user_id"], cur["request_uuid"])
        except fp.ProfileError as exc:
            return _error(exc)
        return jsonify({"ok": True, "scan_request": fsi.public_view(cur)})

    # ---------------------------------------------------------------- guest

    @bp.route("/f/<token>", methods=["GET"])
    @bp.route("/face-scan/request/<token>", methods=["GET"])
    def face_scan_guest_entry(token):
        """The link from the message (``/f/`` is the one sent; the long path
        is kept for links already out). Verified here, then never seen again."""
        if not _guest_limiter.allow(_client_ip()):
            return _guest_page("face_scan_guest.html", 429, state="busy")
        db = _db()
        row = fsi.by_token(db, token)
        if not row:
            return _guest_page("face_scan_guest.html", 404, state="invalid")
        row = fsi.refresh(db, row)
        if row["status"] == fsi.ST_COMPLETED:
            return _guest_page("face_scan_guest.html", 410, state="completed",
                               guest=fsi.guest_view(db, row))
        if not fsi.is_usable(row):
            return _guest_page("face_scan_guest.html", 410, state="inactive")
        row = fsi.mark_opened(db, row)
        session[SESSION_KEY] = {"uuid": row["request_uuid"], "hash": row["token_hash"],
                                "csrf": secrets.token_urlsafe(32)}
        return _guest_headers(redirect(url_for("main.face_scan_guest_consent"), 303))

    @bp.route("/face-scan/guest", methods=["GET"])
    def face_scan_guest_consent():
        db = _db()
        row = _guest_row(db)
        if not row:
            return _guest_page("face_scan_guest.html", 404, state="invalid")
        if row["status"] == fsi.ST_COMPLETED:
            return _guest_page("face_scan_guest.html", 410, state="completed",
                               guest=fsi.guest_view(db, row))
        if not fsi.is_usable(row):
            return _guest_page("face_scan_guest.html", 410, state="inactive")
        return _guest_page("face_scan_guest.html", 200, state="consent",
                           guest=fsi.guest_view(db, row), csrf=session[SESSION_KEY]["csrf"])

    @bp.route("/face-scan/guest/consent", methods=["POST"])
    def face_scan_guest_consent_post():
        db = _db()
        row = _guest_row(db)
        if not row or not fsi.is_usable(row):
            return _guest_page("face_scan_guest.html", 410, state="inactive")
        if not _guest_csrf_ok():
            current_app.logger.warning("FACE_SCAN_REQUEST:GUEST_CSRF_REJECTED request=%s route=consent",
                                       row["request_uuid"])
            return _guest_page("face_scan_guest.html", 403, state="invalid")
        if not request.form.get("consent"):
            return _guest_page("face_scan_guest.html", 200, state="consent",
                               guest=fsi.guest_view(db, row), error="consent_required",
                               csrf=session[SESSION_KEY]["csrf"])
        fsi.record_consent(db, row)
        return _guest_headers(redirect(url_for("main.face_scan_guest_scan"), 303))

    @bp.route("/face-scan/guest/scan", methods=["GET"])
    def face_scan_guest_scan():
        """The same scanner the owner uses, pointed at the guest save route."""
        db = _db()
        row = _guest_row(db)
        if not row or not fsi.is_usable(row):
            return _guest_page("face_scan_guest.html", 410, state="inactive")
        if row.get("consent_at") is None:
            return _guest_headers(redirect(url_for("main.face_scan_guest_consent"), 303))
        g = fsi.guest_view(db, row)
        return _guest_page("tryon.html", 200, scan_for=None, guest_scan={
            "api_base": "/face-scan/guest/api",
            "profile_name": g["profile_name"],
            "sender_name": g["sender_name"],
            "done_url": url_for("main.face_scan_guest_done"),
            "csrf": session[SESSION_KEY]["csrf"],
        })

    @bp.route("/face-scan/guest/api/save", methods=["POST"])
    def face_scan_guest_save():
        db = _db()
        row = _guest_row(db)
        if not row or not fsi.is_usable(row):
            return _guest_headers(jsonify({"ok": False, "error": "inactive",
                                           "message": "This scan request is no longer active"})), 410
        if not _guest_csrf_ok():
            current_app.logger.warning("FACE_SCAN_REQUEST:GUEST_CSRF_REJECTED request=%s route=save",
                                       row["request_uuid"])
            return _guest_headers(jsonify({"ok": False, "error": "forbidden"})), 403
        data = request.get_json(silent=True)
        if not data:
            return _guest_headers(jsonify({"ok": False, "error": "no_data"})), 400
        img = None
        b64 = data.get("screenshot")
        if b64:
            try:
                img = base64.b64decode(b64.split(",", 1)[1] if "," in b64 else b64)
            except Exception:  # noqa: BLE001 - a bad capture is not a bad scan
                img = None
        try:
            capture = fp.store_capture(current_app.root_path, row["customer_id"], img) if img else None
            data["frame_candidates_json"] = (json.dumps(data.get("frame_candidates"))
                                             if data.get("frame_candidates") else None)
            sid = fsi.complete(db, row, data, capture_path=capture,
                               algorithm_version=str(data.get("algorithm_version") or "tryon-7.3"),
                               completed_ip=_client_ip())
        except fp.ProfileError as exc:
            resp, status = _error(exc)
            return _guest_headers(resp), status
        current_app.logger.info("FACE_SCAN_REQUEST:COMPLETED request=%s scan=%s",
                                row["request_uuid"], sid)
        session.pop(SESSION_KEY, None)
        fpa.notify_done(db, sid, row.get("site_host") or request.host, fp.SRC_REMOTE)
        return _guest_headers(jsonify({"ok": True, "scan_id": sid,
                                       "redirect": url_for("main.face_scan_guest_done")}))

    @bp.route("/face-scan/guest/done", methods=["GET"])
    def face_scan_guest_done():
        return _guest_page("face_scan_guest.html", 200, state="done")

    @bp.route("/face-scan/guest/api/my-measurements", methods=["GET"])
    def face_scan_guest_measurements():
        return _guest_headers(jsonify({"has_measurements": False}))

    @bp.route("/face-scan/guest/api/matching-frames", methods=["GET"])
    def face_scan_guest_matching():
        abort(404)
