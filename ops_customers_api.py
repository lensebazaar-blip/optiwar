"""Bearer-authenticated EU Ops endpoints for Optiwar customers."""

from flask import current_app, jsonify, request

from . import ops_customers as customers
from .db import get_db
from .order_face_scan import _ops_auth


def _unauthorized():
    return jsonify({"error": "unauthorized"}), 401


def _customer_error(exc):
    return jsonify({"error": exc.code, "message": str(exc)}), exc.status


def register(bp):
    @bp.route("/ops/api/customers/lookup", methods=["GET"])
    def ops_customers_lookup():
        if not _ops_auth():
            return _unauthorized()
        email = request.args.get("email", "")
        phone = request.args.get("phone", "")
        if not email and not phone:
            return jsonify({"matches": []})
        if phone and len(customers.phone_digits(phone)) < 10:
            return jsonify({"error": "bad_phone",
                            "message": "Enter at least 10 phone digits"}), 400
        return jsonify({"matches": customers.find_customers(
            get_db(), email=email, phone=phone)})

    @bp.route("/ops/api/customers", methods=["POST"])
    def ops_customers_create():
        if not _ops_auth():
            return _unauthorized()
        data = request.get_json(silent=True) or {}
        try:
            result = customers.create_customer(
                get_db(), data.get("name"), data.get("email"), data.get("phone"),
                created_by=data.get("created_by"))
        except customers.CustomerExists as exc:
            return jsonify({"error": "exists", "matches": exc.matches}), 409
        except customers.CustomerError as exc:
            return _customer_error(exc)
        return jsonify(result), 201

    @bp.route("/ops/api/customers/<int:customer_id>/face-scan-request",
              methods=["POST"])
    def ops_customers_face_scan_request(customer_id):
        if not _ops_auth():
            return _unauthorized()
        data = request.get_json(silent=True) or {}
        try:
            result = customers.request_face_scan(
                get_db(), customer_id, data.get("channels"),
                created_by=data.get("created_by"))
        except customers.CustomerError as exc:
            return _customer_error(exc)
        return jsonify(result), 201

    @bp.route("/ops/api/customers/<int:customer_id>/face-scan-request",
              methods=["GET"])
    def ops_customers_face_scan_status(customer_id):
        if not _ops_auth():
            return _unauthorized()
        try:
            result = customers.scan_request_status(get_db(), customer_id)
        except customers.CustomerError as exc:
            return _customer_error(exc)
        return jsonify(result)

    @bp.route("/ops/api/customers/message-preview", methods=["GET"])
    def ops_customers_message_preview():
        if not _ops_auth():
            return _unauthorized()
        try:
            preview = customers.message_preview(
                request.args.get("kind", ""),
                request.args.get("name", ""),
                request.args.get("email", ""),
                request.args.get("phone", ""),
                bool(current_app.config.get("MSG91_LOGIN_OTP_WA_TEMPLATE")))
        except customers.CustomerError as exc:
            return _customer_error(exc)
        return jsonify(preview)
