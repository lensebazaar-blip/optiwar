"""Routes for an uploaded prescription: the customer's reading, Ops' access.

Attached to the main blueprint by ``register(bp)`` (the way ``ops_refunds``
is), so the module deploys without touching the package's ``__init__``.

Customer side — ``POST /contact-lenses/rx-upload`` — is the one step between
a photograph and the eye cards: the file is normalised and kept, the provider
reads it, the lens's validator judges the reading, and what comes back is a
proposal for ``owApplyPrescription`` in the browser plus the same proposal
parked in the session for a reload. It does not touch the cart.

Ops side — ``/api/ops/lens-documents/...`` — sees the document's facts, gets
a signed link to the stored pages that expires in minutes, and can record the
KET ticket a document was referred under. Every issue and every download is
an audit row naming who.
"""
import datetime
import hashlib
import hmac
import os
import time

from flask import (current_app, jsonify, request, send_file, session,
                   url_for)

from . import ai_client, dev_defects, lens_documents, lens_order, lens_rx
from .catalogue import SITE_COM, SITE_IN
from .db import get_db

LINK_TTL_SECONDS = 10 * 60
FIELD = "document"


def _upload_dir():
    root = current_app.root_path
    path = os.path.join(os.path.dirname(root), "secure_uploads", "lens_rx")
    os.makedirs(path, exist_ok=True)
    return path


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or ""


def _actor():
    return session.get("user_email") or (
        "bearer_token" if request.headers.get("Authorization", "")
        .startswith("Bearer ") else None)


def _ops_auth():
    """Ops' own gate (admin session or ``OPS_API_TOKEN``). Fails closed when
    the module that owns it is not present."""
    try:
        from .ops import _require_ops_auth  # noqa: PLC0415 - production-only module
    except ImportError:
        current_app.logger.error("ops module missing; lens-document Ops API disabled")
        return False
    return bool(_require_ops_auth())


def _models():
    from . import models  # noqa: PLC0415 - registered onto its blueprint
    return models


def provider_name():
    return lens_documents.provider_name(
        current_app.config.get("LENS_RX_VISION_PROVIDER")
        or os.environ.get("LENS_RX_VISION_PROVIDER"))


def _refuse(code, message, status=400, where=""):
    dev_defects.record(code, where=where or "rx_upload")
    return jsonify({"ok": False, "code": code, "message": message}), status


def read_document(pages, workload):
    """Ask the configured provider; ``(proposal, confidence, model, unreadable)``.

    Provider failures propagate as ``ai_client.ModelError`` for the route to
    turn into one refusal code; anything the model says that is not the JSON
    asked for is an unreadable document, not an error.
    """
    resp = ai_client.call_model(
        workload=workload, messages=lens_documents.messages_for(pages),
        max_tokens=400, temperature=0, endpoint="/contact-lenses/rx-upload")
    text = ""
    try:
        text = resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        pass
    proposal, confidence, unreadable = lens_documents.parse_reading(text)
    return proposal, confidence, getattr(resp, "model", None), unreadable


def _store_pages(document_id, pages):
    root = _upload_dir()
    total = 0
    for index, data in enumerate(pages):
        with open(os.path.join(root, lens_documents.stored_name(document_id, index)),
                  "wb") as fh:
            fh.write(data)
        total += len(data)
    return lens_documents.stored_name(document_id, 0), total


def _sign(document_id, page, expires):
    key = (current_app.config.get("SECRET_KEY") or "").encode("utf-8")
    msg = ("cldoc:%d:%d:%d" % (int(document_id), int(page), int(expires))).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def signed_path(document_id, page=0, ttl=LINK_TTL_SECONDS, now=None):
    expires = int((now or time.time()) + ttl)
    return url_for("main.lens_document_file", document_id=int(document_id),
                   page=int(page), exp=expires,
                   sig=_sign(document_id, page, expires))


def link_valid(document_id, page, exp, sig, now=None):
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return False
    if exp < int(now or time.time()):
        return False
    return hmac.compare_digest(_sign(document_id, page, exp), str(sig or ""))


def purge_expired(db, today=None, now=None):
    """Delete the stored pages of every document past retention and mark the
    row purged; the row and its audit trail stay. Returns the ids purged.
    Meant for the daily cron; safe to re-run."""
    cursor = db.cursor()
    purged = []
    root = _upload_dir()
    for row in lens_documents.due_for_purge(cursor, today):
        document_id = int(row["document_id"])
        for page in range(int(row.get("pages") or 1)):
            path = os.path.join(root, lens_documents.stored_name(document_id, page))
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                dev_defects.record("RX_UPLOAD_PURGE_FAILED",
                                   where="document_id=%d %s" % (document_id,
                                                                type(exc).__name__))
                break
        else:
            lens_documents.mark_purged(cursor, document_id, now)
            lens_documents.audit(cursor, document_id, lens_documents.AUD_PURGED,
                                 actor="retention", detail="files removed", now=now)
            purged.append(document_id)
    db.commit()
    return purged


def register(bp):
    """Attach the upload and Ops endpoints to the main blueprint."""

    @bp.route("/contact-lenses/rx-upload", methods=["POST"])
    def lens_rx_upload():
        models = _models()
        if models.current_site() == SITE_IN:
            return "Not found", 404
        customer_id = session.get("user_id")
        if not customer_id:
            return jsonify({"ok": False, "code": "RX_UPLOAD_SIGN_IN",
                            "message": "Sign in to upload a prescription; it is "
                                       "kept with your account."}), 401
        db = get_db()
        cursor = db.cursor()
        lens, _ = models._released_or_previewed_lens(
            cursor, request.form.get("product_id"))
        if not lens:
            return jsonify({"ok": False, "code": "RX_UPLOAD_REJECTED",
                            "message": "Product not found."}), 404
        where = "product_id=%s" % lens["product_id"]
        upload = request.files.get(FIELD)
        if upload is None:
            return _refuse("RX_UPLOAD_REJECTED", "Choose a photo or PDF of your "
                           "prescription first.", where=where)
        try:
            norm = lens_documents.normalise(upload.read(
                lens_documents.MAX_UPLOAD_BYTES + 1))
        except lens_documents.Rejected as exc:
            return _refuse(exc.code, exc.message, where=where)

        now = datetime.datetime.now()
        document_id = lens_documents.insert(cursor, customer_id,
                                            lens["product_id"], SITE_COM, norm, now)
        name, stored = _store_pages(document_id, norm["pages"])
        lens_documents.mark_stored(cursor, document_id, name, stored)
        lens_documents.audit(cursor, document_id, lens_documents.AUD_UPLOADED,
                             actor="customer", ip=_client_ip(),
                             detail="%s pages=%d" % (norm["kind"], len(norm["pages"])))
        db.commit()

        workload = provider_name()
        try:
            proposal, confidence, model, unreadable = read_document(
                norm["pages"], workload)
        except ai_client.ModelError as exc:
            lens_documents.mark_parsed(cursor, document_id, workload, None, None,
                                       None, lens_documents.ST_UNREADABLE,
                                       refusal=type(exc).__name__, now=now)
            lens_documents.audit(cursor, document_id, lens_documents.AUD_PARSE_FAILED,
                                 actor=workload, detail=type(exc).__name__)
            db.commit()
            return _refuse("RX_UPLOAD_PROVIDER_FAILED",
                           "We could not read your prescription right now. Your "
                           "upload is saved; please type the values below.",
                           status=503, where=where + " " + workload)
        if unreadable or not proposal:
            lens_documents.mark_parsed(cursor, document_id, workload, model, None,
                                       confidence, lens_documents.ST_UNREADABLE,
                                       refusal="unreadable", now=now)
            lens_documents.audit(cursor, document_id, lens_documents.AUD_PARSE_FAILED,
                                 actor=workload, detail="unreadable")
            db.commit()
            return _refuse("RX_UPLOAD_UNREADABLE",
                           "We could not make out the values on that image. Try "
                           "a sharper, well-lit photo, or type the values below.",
                           status=422, where=where)

        shape = models._lens_choices(cursor, lens)
        minimums = lens_order.minimums(lens, SITE_COM, models._minimums_waived())
        selections = lens_rx.proposal_selections(proposal, minimums)
        lines, problems = lens_order.validate_detailed(
            shape, lens, selections, site=SITE_COM,
            waived=bool(minimums.get("waived")))
        if problems:
            reasons = sorted({c for c, _ in problems})
            lens_documents.mark_parsed(cursor, document_id, workload, model,
                                       proposal, confidence,
                                       lens_documents.ST_INCOMPATIBLE,
                                       refusal=" ".join(reasons), now=now)
            lens_documents.audit(cursor, document_id, lens_documents.AUD_PARSE_FAILED,
                                 actor=workload, detail=" ".join(reasons)[:255])
            db.commit()
            return _refuse("RX_UPLOAD_INCOMPATIBLE",
                           "We read your prescription, but this lens is not made "
                           "in one of those values. Check the values on the cards "
                           "below or ask us in the chat.",
                           status=422, where=where + " " + " ".join(reasons))

        lens_documents.mark_parsed(cursor, document_id, workload, model, proposal,
                                   confidence, lens_documents.ST_PARSED, now=now)
        lens_documents.audit(cursor, document_id, lens_documents.AUD_PARSED,
                             actor=workload, detail="confidence=%s" % confidence)
        db.commit()
        session[lens_rx.PROPOSAL_SESSION_KEY] = {
            "product_id": str(lens["product_id"]),
            "eyes": proposal,
            "form": lens_rx.proposal_form(proposal, selections),
            "source": "upload",
            "document_id": int(document_id),
            "created_at": now.isoformat(timespec="seconds"),
        }
        session.modified = True
        return jsonify({"ok": True, "document_id": int(document_id),
                        "eyes": proposal, "confidence": confidence,
                        "applied_eyes": [ln["eye"] for ln in lines]})

    @bp.route("/api/ops/lens-documents/<int:document_id>", methods=["GET"])
    def lens_document_view(document_id):
        if not _ops_auth():
            return jsonify({"error": "unauthorized"}), 401
        cursor = get_db().cursor()
        row = lens_documents.by_id(cursor, document_id)
        if not row:
            return jsonify({"error": "not_found"}), 404
        cursor.execute(
            "SELECT action, actor, ip, detail, occurred_at FROM "
            "contact_lens_document_audit WHERE document_id=%s ORDER BY id",
            (document_id,))
        trail = [{k: (v.isoformat() if isinstance(v, datetime.datetime) else v)
                  for k, v in r.items()} for r in cursor.fetchall()]
        return jsonify({"ok": True, "document": lens_documents.ops_view(row),
                        "ket": lens_documents.ket_reference(row),
                        "audit": trail})

    @bp.route("/api/ops/lens-documents/<int:document_id>/link", methods=["POST"])
    def lens_document_link(document_id):
        if not _ops_auth():
            return jsonify({"error": "unauthorized"}), 401
        db = get_db()
        cursor = db.cursor()
        row = lens_documents.by_id(cursor, document_id)
        if not row or not row.get("stored_name") or row.get("purged_at"):
            return jsonify({"error": "no_document"}), 404
        pages = int(row.get("pages") or 1)
        links = [signed_path(document_id, page) for page in range(pages)]
        lens_documents.audit(cursor, document_id, lens_documents.AUD_LINK_ISSUED,
                             actor=_actor(), ip=_client_ip(),
                             detail="pages=%d ttl=%ds" % (pages, LINK_TTL_SECONDS))
        db.commit()
        return jsonify({"ok": True, "expires_in": LINK_TTL_SECONDS, "links": links})

    @bp.route("/ops/lens-documents/<int:document_id>/file/<int:page>",
              methods=["GET"])
    def lens_document_file(document_id, page):
        """The stored page, to whoever holds an unexpired signed link."""
        if not link_valid(document_id, page, request.args.get("exp"),
                          request.args.get("sig")):
            return jsonify({"error": "link_invalid_or_expired"}), 403
        db = get_db()
        cursor = db.cursor()
        row = lens_documents.by_id(cursor, document_id)
        if not row or not row.get("stored_name") or row.get("purged_at"):
            return jsonify({"error": "no_document"}), 404
        if page >= int(row.get("pages") or 1):
            return jsonify({"error": "no_page"}), 404
        path = os.path.join(_upload_dir(), lens_documents.stored_name(document_id, page))
        if not os.path.exists(path):
            return jsonify({"error": "no_document"}), 404
        lens_documents.audit(cursor, document_id, lens_documents.AUD_DOWNLOADED,
                             actor=_actor() or "signed_link", ip=_client_ip(),
                             detail="page=%d" % page)
        db.commit()
        resp = send_file(path, mimetype="image/jpeg")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Disposition"] = (
            'inline; filename="%s"' % lens_documents.stored_name(document_id, page))
        return resp

    @bp.route("/api/ops/lens-documents/<int:document_id>/ket-ref", methods=["POST"])
    def lens_document_ket_ref(document_id):
        if not _ops_auth():
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or request.form
        db = get_db()
        cursor = db.cursor()
        row = lens_documents.by_id(cursor, document_id)
        if not row:
            return jsonify({"error": "not_found"}), 404
        if not lens_documents.set_ket_ref(cursor, document_id, data.get("ket_ref")):
            return jsonify({"error": "ket_ref_invalid"}), 400
        lens_documents.audit(cursor, document_id, lens_documents.AUD_KET_REF,
                             actor=_actor(), ip=_client_ip(),
                             detail=str(data.get("ket_ref"))[:64])
        db.commit()
        row = lens_documents.by_id(cursor, document_id)
        return jsonify({"ok": True, "ket": lens_documents.ket_reference(row)})
