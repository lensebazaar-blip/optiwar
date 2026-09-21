"""The favourites routes: a signed-in customer's saved products, and for a
multi-person account the person each saved frame is for.

Saving and removing need only a login; carrying a person needs the Face
Profile gate, exactly as the cart-line selector does. A browser's
``localStorage`` list reaches the server through ``sync`` and is only ever
added to.
"""
from flask import current_app, jsonify, request, session

from . import face_cart
from . import face_fit
from . import face_profiles as fp
from . import face_profiles_api as fpa
from . import favorites as fav
from .db import get_db


def _db():
    db = get_db()
    fp.ensure_schema(db)
    fav.ensure_schema(db)
    return db


def _error(exc):
    return jsonify({"ok": False, "error": exc.code, "message": str(exc)}), exc.status


def _login():
    if not fpa._customer():
        return jsonify({"ok": False, "error": "login_required"}), 401
    return None


def _body():
    return request.get_json(silent=True) or {}


def register(bp):

    @bp.route("/api/favorites", methods=["GET"])
    def favorites_list():
        refused = _login()
        if refused:
            return refused
        return jsonify({"ok": True, "favorites": fav.list_for(_db(), fpa._customer())})

    @bp.route("/api/favorites", methods=["POST"])
    def favorites_add():
        """Save a product. For a multi-person account a frame is saved for
        the person being shopped for unless ``face_profile_id`` says
        otherwise (0 = nobody); anyone else's favourite carries no person."""
        refused = _login()
        if refused:
            return refused
        body = _body()
        try:
            db = _db()
            person = None
            if fpa.gate_enabled():
                if "face_profile_id" in body:
                    person = body.get("face_profile_id")
                else:
                    row = face_fit.active_profile(db, fpa._customer(), session)
                    person = int(row["id"]) if row else None
                if person and not _is_frame(db, body.get("product_id")):
                    person = None
            saved = fav.add(db, fpa._customer(), body.get("product_id"), person)
            if fpa.gate_enabled():
                _with_fit(db, saved)
        except fp.ProfileError as exc:
            return _error(exc)
        return jsonify({"ok": True, "favorite": saved})

    @bp.route("/api/favorites/<int:product_id>", methods=["DELETE"])
    def favorites_remove(product_id):
        refused = _login()
        if refused:
            return refused
        gone = fav.remove(_db(), fpa._customer(), product_id)
        return jsonify({"ok": True, "removed": gone})

    @bp.route("/api/favorites/sync", methods=["POST"])
    def favorites_sync():
        refused = _login()
        if refused:
            return refused
        try:
            db = _db()
            before = len(fav.list_for(db, fpa._customer()))
            merged = fav.sync(db, fpa._customer(), _body().get("product_ids", []))
        except fp.ProfileError as exc:
            return _error(exc)
        return jsonify({"ok": True, "favorites": merged, "added": len(merged) - before})

    @bp.route("/api/favorites/<int:product_id>/person", methods=["POST"])
    def favorites_assign(product_id):
        """Point a saved frame at one of the customer's people or at nobody,
        and answer with the fit for that person."""
        refused = fpa._require()
        if refused:
            return refused
        try:
            db = _db()
            saved = fav.assign(db, fpa._customer(), product_id,
                               _body().get("face_profile_id"))
            _with_fit(db, saved)
        except fp.ProfileError as exc:
            return _error(exc)
        current_app.logger.info("FAVORITE:PERSON customer=%s product=%s profile=%s",
                                fpa._customer(), product_id, saved["face_profile_id"])
        return jsonify({"ok": True, "favorite": saved, "fit": saved["fit"]})


def _with_fit(db, saved):
    """Add the person and the fit for them to a saved frame; a saved product
    that is not a frame gets neither."""
    cur = db.cursor()
    try:
        product = fav.catalogue(cur, [saved["product_id"]]).get(saved["product_id"])
    finally:
        cur.close()
    if product is None or not face_cart.is_frame_line(product):
        saved["person"] = None
        saved["fit"] = None
        return saved
    fit = face_fit.evaluate_frame_fit(db, fpa._customer(), saved["face_profile_id"], product)
    saved["person"] = fit["profile"]
    saved["fit"] = fit
    return saved


def _is_frame(db, product_id):
    try:
        pid = int(product_id)
    except (TypeError, ValueError):
        return False
    cur = db.cursor()
    try:
        return pid in fav._frame_ids(cur, [pid])
    finally:
        cur.close()
