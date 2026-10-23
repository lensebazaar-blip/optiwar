"""Read-only Ops view of an order's face scan and frame fit."""

import json
import os
from datetime import datetime, timedelta
from urllib.parse import quote

from flask import (current_app, jsonify, render_template, request, send_file,
                   session, has_app_context)

try:
    from . import face_cart, face_fit, face_profiles
    from .db import get_db
except ImportError:
    import face_cart
    import face_fit
    import face_profiles
    from db import get_db


BINOCULAR_WARNING = (
    "Scan PD is binocular (far/near). Monocular PD and fitting height are not "
    "measured by the scan.")
NO_SCAN_WARNING = (
    "No face scan on this account. Ask the customer to scan at "
    "https://optiwar.in/tryon before glazing progressive or bifocal lenses.")


def _ops_auth():
    try:
        from .ops import _require_ops_auth
    except ImportError:
        from ops import _require_ops_auth
    return _require_ops_auth()


def _normalise_order_id(order_id):
    oid = str(order_id or "").strip().upper()
    return oid[3:] if oid.startswith("OW-") else oid


def _datetime(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _iso(value):
    value = _datetime(value)
    return value.isoformat() if value else None


def _retention_days():
    configured = (current_app.config.get("FACE_RAW_CAPTURE_RETENTION_DAYS")
                  if has_app_context() else None)
    if configured is None:
        configured = os.environ.get(
            "FACE_RAW_CAPTURE_RETENTION_DAYS",
            face_profiles.DEFAULT_RAW_CAPTURE_RETENTION_DAYS)
    try:
        return max(0, int(configured))
    except (TypeError, ValueError):
        return face_profiles.DEFAULT_RAW_CAPTURE_RETENTION_DAYS


def _capture_url(order_id, scan_id=None):
    path = "/ops/api/orders/%s/face-scan/capture" % quote(str(order_id), safe="")
    if scan_id is not None:
        path += "?scan_id=%s" % quote(str(scan_id), safe="")
    return path


def _capture_info(name, measured_at, app_root, order_id, scan_id=None,
                  purged_at=None):
    if not name:
        return {"available": False, "url": None, "reason": "none_recorded"}

    basename = os.path.basename(str(name))
    path = os.path.join(face_profiles.capture_dir(app_root), basename)
    if purged_at:
        return {"available": False, "url": None,
                "reason": "purged_after_retention"}
    if os.path.isfile(path):
        return {"available": True,
                "url": _capture_url(order_id, scan_id), "reason": None}

    measured = _datetime(measured_at)
    if measured and measured < datetime.now() - timedelta(days=_retention_days()):
        reason = "purged_after_retention"
    else:
        reason = "missing"
    return {"available": False, "url": None, "reason": reason}


def _recommended_size(measurement):
    dimensions = face_fit.recommended_dimensions(measurement or {})
    return dimensions["size"] if dimensions else None


def _timing(measured_at, order_date):
    measured = _datetime(measured_at)
    placed = _datetime(order_date)
    if not measured or not placed:
        return None, None
    delta = measured - placed
    return measured <= placed, round(delta.total_seconds() / 3600.0, 1)


def _scan_payload(row, order_date, app_root, order_id, scan_id=None,
                  capture_name=None, purged_at=None):
    measured_at = row.get("measured_at")
    before, hours = _timing(measured_at, order_date)
    return {
        "scan_id": int(scan_id) if scan_id is not None else None,
        "pd_far": row.get("pd_far"),
        "pd_near": row.get("pd_near"),
        "face_width": row.get("face_width"),
        "eye_mouth": row.get("eye_mouth"),
        "decentration": row.get("decentration"),
        "recommended_size": _recommended_size(row),
        "measured_at": _iso(measured_at),
        "scanned_before_order": before,
        "hours_from_order": hours,
        "capture": _capture_info(capture_name, measured_at, app_root, order_id,
                                 scan_id=scan_id, purged_at=purged_at),
    }


def _people(cursor, customer_id, order_date, app_root, order_id):
    try:
        cursor.execute(
            """SELECT p.id AS face_profile_id, p.display_name,
                      s.id AS scan_id, s.pd_far, s.pd_near, s.face_width,
                      s.eye_mouth, s.decentration, s.recommended_diameter,
                      s.recommended_bridge, s.recommended_length, s.measured_at,
                      s.capture_path, s.capture_purged_at
               FROM face_profiles p
               LEFT JOIN face_scans s ON s.id=(
                   SELECT s2.id FROM face_scans s2
                   WHERE s2.face_profile_id=p.id AND s2.status='COMPLETED'
                   ORDER BY s2.measured_at DESC, s2.id DESC LIMIT 1)
               WHERE p.customer_id=%s AND p.is_active=1
               ORDER BY p.id""", (int(customer_id),))
        rows = cursor.fetchall()
    except Exception:  # These feature tables are not present on older installs.
        return []

    result = []
    for row in rows:
        scan = None
        if row.get("scan_id") is not None:
            scan = _scan_payload(
                row, order_date, app_root, order_id,
                scan_id=row["scan_id"], capture_name=row.get("capture_path"),
                purged_at=row.get("capture_purged_at"))
        person = {
            "face_profile_id": int(row["face_profile_id"]),
            "display_name": row.get("display_name"),
            "scan": scan,
        }
        if scan:
            person.update({key: scan[key] for key in (
                "scan_id", "pd_far", "pd_near", "face_width",
                "recommended_size", "measured_at", "capture")})
        result.append(person)
    return result


def _decode_snapshot(snapshot):
    measurements = snapshot.get("measurements")
    fit = snapshot.get("fit")
    if isinstance(measurements, str):
        try:
            measurements = json.loads(measurements)
        except (TypeError, ValueError):
            measurements = None
    if isinstance(fit, str):
        try:
            fit = json.loads(fit)
        except (TypeError, ValueError):
            fit = None
    return measurements, fit


def _warn(warnings, message):
    if message not in warnings:
        warnings.append(message)


def scan_for_order(db, order_id, app_root):
    """Return one order's sealed or current account face-scan information."""
    oid = _normalise_order_id(order_id)
    if not oid:
        return None

    cursor = db.cursor()
    try:
        cursor.execute(
            """SELECT o.order_line_id, o.order_id, o.date_created,
                      o.customer_id, o.product_id, o.site_from,
                      p.product_code, p.product_name, p.product_category,
                      p.product_size
               FROM orders o
               LEFT JOIN products p ON p.product_id=o.product_id
               WHERE o.order_id=%s ORDER BY o.order_line_id""", (oid,))
        lines = cursor.fetchall()
        if not lines:
            return None

        actual_order_id = str(lines[0]["order_id"])
        customer_id = lines[0].get("customer_id")
        order_date = lines[0].get("date_created")
        snapshots = []
        try:
            snapshots = face_cart.for_order(cursor, actual_order_id)
        except Exception:  # Snapshot storage was introduced after legacy orders.
            snapshots = []

        account_scan = None
        if customer_id is not None:
            cursor.execute(
                "SELECT * FROM face_measurements WHERE customer_id=%s "
                "ORDER BY measured_at DESC, id DESC LIMIT 1",
                (int(customer_id),))
            account_scan = cursor.fetchone()

        people = _people(cursor, customer_id, order_date, app_root,
                         actual_order_id) if customer_id is not None else []

        snapshot_by_line = {}
        for snapshot in snapshots:
            try:
                key = int(snapshot["line_no"])
                product_id = int(snapshot["product_id"])
            except (KeyError, TypeError, ValueError):
                continue
            snapshot_by_line[key] = (product_id, snapshot)

        warnings = []
        any_scan = bool(account_scan)
        for person in people:
            if person["scan"] is not None:
                any_scan = True
        output_lines = []
        for index, row in enumerate(lines, start=1):
            is_frame = face_cart.is_frame_line({
                "product_category": row.get("product_category")})
            source = None
            person = None
            scan = None
            fit = None
            if is_frame:
                sealed = snapshot_by_line.get(index)
                if sealed and sealed[0] == int(row["product_id"] or 0):
                    snapshot = sealed[1]
                    measurements, sealed_fit = _decode_snapshot(snapshot)
                    has_person = (snapshot.get("face_profile_id") is not None
                                  or snapshot.get("display_name"))
                    if has_person:
                        source = "order_snapshot"
                        person = snapshot.get("display_name") or "Account holder"
                        fit = sealed_fit
                        if measurements is not None:
                            measurements = dict(measurements)
                            measurements.setdefault("measured_at",
                                                     snapshot.get("measured_at"))
                            scan = _scan_payload(
                                measurements, order_date, app_root,
                                actual_order_id,
                                scan_id=snapshot.get("face_scan_id"))
                            if snapshot.get("face_scan_id") is not None:
                                cur = db.cursor()
                                try:
                                    cur.execute(
                                        "SELECT capture_path, capture_purged_at "
                                        "FROM face_scans WHERE id=%s AND customer_id=%s",
                                        (snapshot["face_scan_id"], int(customer_id)))
                                    capture_row = cur.fetchone()
                                except Exception:
                                    capture_row = None
                                finally:
                                    cur.close()
                                if capture_row:
                                    scan["capture"] = _capture_info(
                                        capture_row.get("capture_path"),
                                        measurements.get("measured_at"),
                                        app_root, actual_order_id,
                                        scan_id=snapshot["face_scan_id"],
                                        purged_at=capture_row.get("capture_purged_at"))
                    else:
                        snapshot = None
                else:
                    snapshot = None

                if snapshot is None:
                    if account_scan:
                        source = "account_scan"
                        person = "Account holder"
                        scan = _scan_payload(
                            account_scan, order_date, app_root,
                            actual_order_id,
                            capture_name=account_scan.get("screenshot_path"))
                        fit = face_fit.evaluate(account_scan, row.get("product_size"))
                    else:
                        fit = face_fit.evaluate(None, row.get("product_size"))

            if scan is not None:
                any_scan = True
                if scan.get("scanned_before_order") is False:
                    _warn(warnings, "Scan taken after the order was placed.")
                if scan["capture"]["reason"] == "purged_after_retention":
                    _warn(
                        warnings,
                        "Scan photo deleted after the %d-day retention; "
                        "measurements are kept." % _retention_days())
            if fit and fit.get("classification") == face_fit.NOT_MATCHED:
                rec = (fit.get("recommended_dimensions") or {}).get("size") or "unknown"
                size = row.get("product_size") or "not stated"
                reasons = "; ".join(fit.get("reasons") or [])
                _warn(warnings, "Frame size %s does not match the recommended %s: %s."
                      % (size, rec, reasons))

            output_lines.append({
                "order_line_id": row["order_line_id"],
                "product_id": row.get("product_id"),
                "product_code": row.get("product_code"),
                "product_name": row.get("product_name"),
                "product_size": row.get("product_size"),
                "is_frame": is_frame,
                "source": source,
                "person": person,
                "scan": scan,
                "fit": fit,
            })

        for person_row in people:
            profile_scan = person_row.get("scan")
            if profile_scan:
                if profile_scan.get("scanned_before_order") is False:
                    _warn(warnings, "Scan taken after the order was placed.")
                if profile_scan["capture"]["reason"] == "purged_after_retention":
                    _warn(
                        warnings,
                        "Scan photo deleted after the %d-day retention; "
                        "measurements are kept." % _retention_days())

        if any_scan:
            _warn(warnings, BINOCULAR_WARNING)
        else:
            _warn(warnings, NO_SCAN_WARNING)

        return {
            "ok": True,
            "order_id": actual_order_id,
            "customer_id": customer_id,
            "date_created": _iso(order_date),
            "site_from": lines[0].get("site_from"),
            "status": "SCAN_FOUND" if any_scan else "NO_SCAN",
            "lines": output_lines,
            "people": people,
            "warnings": warnings,
        }
    finally:
        cursor.close()


def _not_found():
    return jsonify({"ok": False, "error": "not_found"}), 404


def register(bp):
    @bp.route("/ops/api/orders/<order_id>/face-scan", methods=["GET"])
    def ops_order_face_scan_api(order_id):
        if not _ops_auth():
            return jsonify({"error": "Unauthorized"}), 401
        result = scan_for_order(get_db(), order_id, current_app.root_path)
        return jsonify(result) if result is not None else _not_found()

    @bp.route("/ops/api/orders/<order_id>/face-scan/capture", methods=["GET"])
    def ops_order_face_scan_capture(order_id):
        if not _ops_auth():
            return jsonify({"error": "Unauthorized"}), 401
        db = get_db()
        result = scan_for_order(db, order_id, current_app.root_path)
        if result is None:
            return _not_found()

        scan_id = request.args.get("scan_id")
        cursor = db.cursor()
        try:
            if scan_id is not None:
                try:
                    scan_key = int(scan_id)
                except (TypeError, ValueError):
                    return _not_found()
                cursor.execute(
                    "SELECT capture_path, capture_purged_at FROM face_scans "
                    "WHERE id=%s AND customer_id=%s",
                    (scan_key, int(result["customer_id"])))
                row = cursor.fetchone()
                if not row or row.get("capture_purged_at") or not row.get("capture_path"):
                    return _not_found()
                stored_name = row["capture_path"]
            else:
                cursor.execute(
                    "SELECT screenshot_path FROM face_measurements "
                    "WHERE customer_id=%s ORDER BY measured_at DESC, id DESC LIMIT 1",
                    (int(result["customer_id"]),))
                row = cursor.fetchone()
                stored_name = row.get("screenshot_path") if row else None
        except Exception:
            return _not_found()
        finally:
            cursor.close()

        if not stored_name:
            return _not_found()
        path = os.path.join(face_profiles.capture_dir(current_app.root_path),
                            os.path.basename(str(stored_name)))
        if not os.path.isfile(path):
            return _not_found()
        actor = session.get("user_email") or "bearer"
        current_app.logger.info(
            "OPS_FACE_CAPTURE_VIEW order:%s customer:%s by:%s",
            result["order_id"], result["customer_id"], actor)
        response = send_file(path, mimetype="image/jpeg", max_age=0)
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.route("/ops/face-scan", methods=["GET"])
    def ops_order_face_scan_page():
        if not _ops_auth():
            return jsonify({"error": "Unauthorized"}), 401
        order = request.args.get("order", "")
        result = (scan_for_order(get_db(), order, current_app.root_path)
                  if order.strip() else None)
        return render_template("ops_order_face_scan.html",
                               order=order, result=result,
                               not_found=bool(order.strip() and result is None)), (
                                   404 if order.strip() and result is None else 200)
