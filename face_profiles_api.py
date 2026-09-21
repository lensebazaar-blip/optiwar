"""
The customer-facing Face Profile API and the Stage-1 gate.

Every object reference is resolved as ``(authenticated customer, id)``; an id
that belongs to somebody else answers 404 exactly as a missing one does, so a
customer cannot learn that another customer's profile exists. Captures are
served only from here, only to their owner, from outside the web root.
"""
import os

from flask import current_app, jsonify, request, send_file, session

from . import face_fit
from . import face_profiles as fp
from . import face_scan_done as fsd
from . import face_scan_groups as fsg
from . import face_scan_invites as fsi
from .catalogue import sellable_here
from .db import get_db

# Read from the environment, not app.config: __init__.py is outside the
# deployment set, so the gate must be settable without editing it.
ENABLED_ENV = "FACE_PROFILES_ENABLED"
ALLOW_ENV = "FACE_PROFILES_ALLOW_EMAILS"


def gate_enabled(environ=None):
    """Whether the signed-in customer sees the multi-person experience."""
    env = os.environ if environ is None else environ
    return fp.enabled_for(session.get("user_email"),
                          current_app.config.get(ENABLED_ENV, env.get(ENABLED_ENV)),
                          current_app.config.get(ALLOW_ENV, env.get(ALLOW_ENV)))


def _customer():
    return session.get("user_id")


def _account_name():
    return session.get("user_name") or ""


def _db():
    db = get_db()
    fp.ensure_schema(db)
    return db


def _view(row):
    url = None
    if row.get("capture_path"):
        url = "/api/face-profiles/%d/capture" % int(row["id"])
    return fp.public_view(row, url)


def _error(exc):
    return jsonify({"ok": False, "error": exc.code,
                    "message": str(exc)}), exc.status


def _require():
    """401 for anonymous, 404 for gated-off — the feature does not exist for
    an account outside the allow-list, and says so the way a missing page
    would."""
    if not _customer():
        return jsonify({"ok": False, "error": "login_required"}), 401
    if not gate_enabled():
        return jsonify({"ok": False, "error": "not_found"}), 404
    return None


def register(bp):
    """Attach the profile routes to an already-registered blueprint."""

    @bp.route("/api/face-profiles", methods=["GET"])
    def face_list_profiles():
        refused = _require()
        if refused:
            return refused
        db = _db()
        cid = _customer()
        fp.migrate_customer(db, cid, _account_name())
        rows = fp.list_profiles(db, cid)
        return jsonify({"ok": True, "profiles": [_view(r) for r in rows],
                        "relationships": [
                            {"code": c, "label": fp.RELATIONSHIP_LABELS[c]}
                            for c in fp.RELATIONSHIPS if c != fp.REL_SELF]})


    @bp.route("/api/face-profiles", methods=["POST"])
    def face_create_profile():
        refused = _require()
        if refused:
            return refused
        data = request.get_json(silent=True) or {}
        db = _db()
        try:
            row = fp.create_profile(db, _customer(), data.get("display_name"),
                                    data.get("relationship_type"),
                                    consent=bool(data.get("consent")),
                                    account_name=_account_name())
        except fp.ProfileError as exc:
            return _error(exc)
        current_app.logger.info("FACE_PROFILE:CREATED customer=%s profile=%s rel=%s",
                                _customer(), row["id"], row["relationship_type"])
        return jsonify({"ok": True, "profile": _view(row)}), 201


    @bp.route("/api/face-profiles/<int:profile_id>", methods=["GET"])
    def face_get_profile(profile_id):
        refused = _require()
        if refused:
            return refused
        try:
            row = fp.get_profile(_db(), _customer(), profile_id)
        except fp.ProfileError as exc:
            return _error(exc)
        return jsonify({"ok": True, "profile": _view(row)})


    @bp.route("/api/face-profiles/<int:profile_id>", methods=["PATCH", "POST"])
    def face_update_profile(profile_id):
        refused = _require()
        if refused:
            return refused
        data = request.get_json(silent=True) or {}
        try:
            row = fp.rename_profile(_db(), _customer(), profile_id,
                                    display_name=data.get("display_name"),
                                    relationship_type=data.get("relationship_type"))
        except fp.ProfileError as exc:
            return _error(exc)
        return jsonify({"ok": True, "profile": _view(row)})


    @bp.route("/api/face-profiles/<int:profile_id>/default", methods=["POST"])
    def face_set_default(profile_id):
        refused = _require()
        if refused:
            return refused
        try:
            row = fp.set_default(_db(), _customer(), profile_id)
        except fp.ProfileError as exc:
            return _error(exc)
        current_app.logger.info("FACE_PROFILE:DEFAULT customer=%s profile=%s",
                                _customer(), row["id"])
        return jsonify({"ok": True, "profile": _view(row)})


    @bp.route("/api/face-profiles/<int:profile_id>/references", methods=["GET"])
    def face_profile_references(profile_id):
        refused = _require()
        if refused:
            return refused
        try:
            refs = fp.references(_db(), _customer(), profile_id)
        except fp.ProfileError as exc:
            return _error(exc)
        return jsonify({"ok": True, "references": refs})


    @bp.route("/api/face-profiles/<int:profile_id>", methods=["DELETE"])
    def face_delete_profile(profile_id):
        refused = _require()
        if refused:
            return refused
        try:
            db = _db()
            fp.require_profile(db, _customer(), profile_id)
            fsi.ensure_schema(db)
            cancelled = fsi.cancel_for_profile(db, _customer(), profile_id)
            fsg.ensure_schema(db)
            left = fsg.drop_profile(db, _customer(), profile_id, notifier=group_notifier())
            result = fp.delete_profile(db, _customer(), profile_id)
            result["cancelled_scan_requests"] = cancelled
            result["left_scan_groups"] = left
        except fp.ProfileError as exc:
            return _error(exc)
        current_app.logger.info("FACE_PROFILE:DELETED customer=%s profile=%s",
                                _customer(), profile_id)
        return jsonify({"ok": True, **result})


    @bp.route("/api/face-context", methods=["GET"])
    def face_context_get():
        """Who the customer is shopping for, and who else they could pick."""
        refused = _require()
        if refused:
            return refused
        db = _db()
        fp.migrate_customer(db, _customer(), _account_name())
        return jsonify({"ok": True, **face_fit.context(db, _customer(), session)})


    @bp.route("/api/face-context", methods=["POST"])
    def face_context_set():
        """Choose who to shop for on this device: one of the customer's own
        profiles, or nobody (``face_profile_id`` null / 0 = No person / Gift).
        Somebody else's id is 404. Optionally also returns the fit of one
        product (``product_id``) for the new choice, so a page can update
        without a second request."""
        refused = _require()
        if refused:
            return refused
        data = request.get_json(silent=True) or {}
        db = _db()
        try:
            face_fit.set_active(db, _customer(), session, data.get("face_profile_id"))
        except fp.ProfileError as exc:
            return _error(exc)
        session.modified = True
        out = face_fit.context(db, _customer(), session)
        if isinstance(data.get("product_id"), int) and data["product_id"] > 0:
            out["fit"] = _fit_for(db, data["product_id"], out["active_profile_id"])
        current_app.logger.info("FACE_CONTEXT:SET customer=%s profile=%s",
                                _customer(), out["active_profile_id"])
        return jsonify({"ok": True, **out})


    @bp.route("/api/frames/<int:product_id>/fit", methods=["GET"])
    def face_frame_fit(product_id):
        """The engine's verdict on one frame for one of the customer's
        profiles (``?face_profile_id=``; absent = the active one; 0 = nobody).
        Somebody else's profile, or a product not sold here, is 404."""
        refused = _require()
        if refused:
            return refused
        db = _db()
        pid = request.args.get("face_profile_id")
        if pid is None:
            active = face_fit.active_profile(db, _customer(), session)
            pid = int(active["id"]) if active else 0
        try:
            fit = _fit_for(db, product_id, pid)
        except fp.ProfileError as exc:
            return _error(exc)
        if fit is None:
            return jsonify({"ok": False, "error": "not_found"}), 404
        return jsonify({"ok": True, "fit": fit})


    @bp.route("/api/face-profiles/<int:profile_id>/capture", methods=["GET"])
    def face_profile_capture(profile_id):
        """The owner's own capture, from the secure directory. Anyone else: 404."""
        refused = _require()
        if refused:
            return refused
        db = _db()
        try:
            row = fp.get_profile(db, _customer(), profile_id)
        except fp.ProfileError as exc:
            return _error(exc)
        name = row.get("capture_path")
        if not name or row.get("capture_purged_at"):
            return jsonify({"ok": False, "error": "not_found"}), 404
        path = os.path.join(fp.capture_dir(current_app.root_path),
                            os.path.basename(name))
        if not os.path.isfile(path):
            return jsonify({"ok": False, "error": "not_found"}), 404
        resp = send_file(path, mimetype="image/jpeg", max_age=0)
        resp.headers["Cache-Control"] = "private, no-store"
        return resp


def _fit_for(db, product_id, profile_id):
    """The fit of one frame sold on this storefront, or None for no such
    frame here; a foreign profile raises ``fp.NotFound``."""
    cur = db.cursor()
    try:
        if not sellable_here(cur, product_id):
            return None
        cur.execute("SELECT product_id, product_code, product_name, product_size "
                    "FROM products WHERE product_id=%s", (int(product_id),))
        product = cur.fetchone()
    finally:
        cur.close()
    if not product:
        return None
    fit = face_fit.evaluate_frame_fit(db, _customer(), profile_id, product)
    fit["product"] = {"product_id": int(product["product_id"]),
                      "product_code": product["product_code"],
                      "product_size": product["product_size"]}
    return fit


def shopping_for(db):
    """For page renders: the active profile row of a gated-on customer, or
    None (no customer, gate off, or No person)."""
    if not _customer() or not gate_enabled():
        return None
    fp.ensure_schema(db)
    fp.migrate_customer(db, _customer(), _account_name())
    return face_fit.active_profile(db, _customer(), session)


def _notify_env():
    env = dict(os.environ)
    for key in (ENABLED_ENV, ALLOW_ENV, fsi.ENABLED_ENV, fsi.ALLOW_ENV,
                fsd.ENABLED_ENV, fsd.WA_TEMPLATE_ENV):
        if key in current_app.config:
            env[key] = current_app.config[key]
    return env


def notify_done(db, scan_id, site_host, source):
    """After the scan's own commit: tell the owner, once; never fail the save."""
    env = _notify_env()
    try:
        out = fsd.notify(db, scan_id, site_host, source=source, environ=env)
    except Exception as exc:  # noqa: BLE001 - a notification must not undo a saved scan
        current_app.logger.warning("FACE_SCAN_DONE:ERROR scan=%s err=%s", scan_id,
                                   str(exc)[:160])
        return None
    current_app.logger.info("FACE_SCAN_DONE scan=%s %s", scan_id, out)
    return out


def group_notifier():
    """The group completion notice with this app's gate settings applied."""
    env = _notify_env()
    return lambda db, row: fsg.notify(db, row, environ=env)


def group_scan_landed(db, customer_id, profile_id, scan_id, scan_group_id=None):
    """After a scan's own commit: advance every open group this person is a
    member of (a local try-on scan counts as much as a link's); never fail
    the save."""
    try:
        fsg.ensure_schema(db)
        views = fsg.on_profile_scanned(db, customer_id, profile_id, scan_id, scan_group_id,
                                       notifier=group_notifier())
    except Exception as exc:  # noqa: BLE001 - the scan is saved; the group is bookkeeping
        current_app.logger.warning("FACE_SCAN_GROUP:ERROR group=%s scan=%s err=%s",
                                   scan_group_id, scan_id, str(exc)[:160])
        return None
    for out in views:
        current_app.logger.info("FACE_SCAN_GROUP:PROGRESS group=%s %s/%s status=%s",
                                out["group_uuid"], out["completed"], out["required"], out["status"])
    return views[0] if views else None
    return out


def save_scan_from_tryon(db, data, capture_bytes):
    """What ``/api/tryon/save`` does for a gated-on customer.

    The profile is the one the customer chose before scanning
    (``face_profile_id``), or their default when the page was opened without
    one. Somebody else's id is a 404, not a fallback.
    """
    cid = _customer()
    fp.ensure_schema(db)
    fp.migrate_customer(db, cid, _account_name())
    pid = data.get("face_profile_id")
    if pid in (None, ""):
        d = fp.default_profile(db, cid)
        pid = d["id"] if d else fp.ensure_self(db, cid, _account_name())["id"]
    capture = None
    if capture_bytes:
        capture = fp.store_capture(current_app.root_path, cid, capture_bytes)
    sid = fp.record_scan(db, cid, pid, data, capture_path=capture,
                         algorithm_version=str(data.get("algorithm_version")
                                               or "tryon-7.2"))
    row = fp.get_profile(db, cid, pid)
    notify_done(db, sid, request.host, fp.SRC_TRYON)
    group_scan_landed(db, cid, int(pid), sid)
    return sid, row
