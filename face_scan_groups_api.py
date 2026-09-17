"""Owner routes for scan groups. Same gate, same 404 rule as the rest of
My Faces: a uuid that is not yours does not exist.

    POST   /api/face-scan-groups                                   {profile_ids}
    GET    /api/face-scan-groups                                   open groups
    GET    /api/face-scan-groups/<uuid>
    POST   /api/face-scan-groups/<uuid>/cancel
    POST   /api/face-scan-groups/<uuid>/members                    {face_profile_id}
    DELETE /api/face-scan-groups/<uuid>/members/<pid>
    POST   /api/face-scan-groups/<uuid>/members/<pid>/scan
             {channel, destination}  -> a remote invitation bound to the group
             {pd_far, pd_near, ...}  -> the owner's own scan of that member, now
"""
import base64
import json

from flask import current_app, jsonify, request, session

from . import face_profiles as fp
from . import face_profiles_api as fpa
from . import face_scan_groups as fsg
from . import face_scan_invites as fsi
from . import face_scan_invites_api as fsia


def _db():
    db = fsia._db()
    fsg.ensure_schema(db)
    return db


def _cid():
    return session["user_id"]


def register(bp):

    @bp.route("/api/face-scan-groups", methods=["POST"])
    def face_scan_group_create():
        refused = fsia._require()
        if refused:
            return refused
        data = request.get_json(silent=True) or {}
        try:
            row = fsg.create(_db(), _cid(), data.get("profile_ids"), site_host=fsia._site_host())
        except fp.ProfileError as exc:
            return fsia._error(exc)
        current_app.logger.info("FACE_SCAN_GROUP:CREATED customer=%s group=%s",
                                _cid(), row["group_uuid"])
        return jsonify({"ok": True, "group": fsg.view(_db(), row)}), 201

    @bp.route("/api/face-scan-groups", methods=["GET"])
    def face_scan_group_list():
        refused = fsia._require()
        if refused:
            return refused
        return jsonify({"ok": True, "groups": fsg.for_customer(_db(), _cid())})

    @bp.route("/api/face-scan-groups/<group_uuid>", methods=["GET"])
    def face_scan_group_get(group_uuid):
        refused = fsia._require()
        if refused:
            return refused
        try:
            return jsonify({"ok": True, "group": fsg.get(_db(), _cid(), group_uuid)})
        except fp.ProfileError as exc:
            return fsia._error(exc)

    @bp.route("/api/face-scan-groups/<group_uuid>/cancel", methods=["POST"])
    def face_scan_group_cancel(group_uuid):
        refused = fsia._require()
        if refused:
            return refused
        try:
            return jsonify({"ok": True, "group": fsg.cancel(_db(), _cid(), group_uuid)})
        except fp.ProfileError as exc:
            return fsia._error(exc)

    @bp.route("/api/face-scan-groups/<group_uuid>/members", methods=["POST"])
    def face_scan_group_add_member(group_uuid):
        refused = fsia._require()
        if refused:
            return refused
        data = request.get_json(silent=True) or {}
        try:
            g = fsg.add_member(_db(), _cid(), group_uuid, data.get("face_profile_id"))
        except fp.ProfileError as exc:
            return fsia._error(exc)
        return jsonify({"ok": True, "group": g})

    @bp.route("/api/face-scan-groups/<group_uuid>/members/<int:profile_id>", methods=["DELETE"])
    def face_scan_group_remove_member(group_uuid, profile_id):
        refused = fsia._require()
        if refused:
            return refused
        try:
            g = fsg.remove_member(_db(), _cid(), group_uuid, profile_id,
                                  notifier=fpa.group_notifier())
        except fp.ProfileError as exc:
            return fsia._error(exc)
        return jsonify({"ok": True, "group": g})

    @bp.route("/api/face-scan-groups/<group_uuid>/members/<int:profile_id>/scan", methods=["POST"])
    def face_scan_group_member_scan(group_uuid, profile_id):
        refused = fsia._require()
        if refused:
            return refused
        data = request.get_json(silent=True) or {}
        db = _db()
        try:
            row = fsg.require_group(db, _cid(), group_uuid)
            if row["status"] != fsg.ST_OPEN:
                raise fsg.GroupError("closed", "This scan group is %s" % row["status"].lower(), 409)
            if not any(int(m["face_profile_id"]) == int(profile_id) for m in fsg.members(db, row)):
                raise fsg.GroupError("not_member", "That person is not in this scan group", 404)
            if data.get("channel"):
                inv, link = fsia._create_and_send(db, _cid(), profile_id, data.get("channel"),
                                                  data.get("destination"),
                                                  scan_group_id=row["group_uuid"])
                return jsonify({"ok": True, "scan_request": fsi.public_view(inv),
                                "link": link, "group": fsg.get(db, _cid(), group_uuid)}), 201
            img = None
            b64 = data.get("screenshot")
            if b64:
                try:
                    img = base64.b64decode(b64.split(",", 1)[1] if "," in b64 else b64)
                except Exception:  # noqa: BLE001 - a bad capture is not a bad scan
                    img = None
            capture = fp.store_capture(current_app.root_path, _cid(), img) if img else None
            data["frame_candidates_json"] = (json.dumps(data.get("frame_candidates"))
                                             if data.get("frame_candidates") else None)
            sid = fsg.record_member_scan(
                db, _cid(), group_uuid, profile_id, data, capture_path=capture,
                algorithm_version=str(data.get("algorithm_version") or "tryon-7.3"),
                on_landed=fpa.group_scan_landed)
        except fp.ProfileError as exc:
            return fsia._error(exc)
        fpa.notify_done(db, sid, request.host, fp.SRC_TRYON)
        return jsonify({"ok": True, "scan_id": sid, "group": fsg.get(db, _cid(), group_uuid)})
