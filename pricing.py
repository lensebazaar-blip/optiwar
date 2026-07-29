"""
Lens Pricing Management Blueprint
- JSON config file as single source of truth
- Admin UI at /ops/pricing to view and update prices
- API endpoint to serve pricing to frontend dynamically
"""
import os
import json
from flask import Blueprint, request, jsonify, render_template_string, session, redirect
from functools import wraps

bp = Blueprint("pricing", __name__)

PRICING_FILE = "/var/www/flask-optiwar-ow-release-090525/lens_pricing.json"

def load_pricing():
    try:
        with open(PRICING_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_pricing(data):
    with open(PRICING_FILE, "w") as f:
        json.dump(data, f, indent=2)

@bp.route("/api/lens-pricing", methods=["GET"])
def get_pricing_api():
    data = load_pricing()
    return jsonify(data)

# ─── API endpoints for eu.lensbazaar.com dashboard (shared secret) ────
API_SECRET = os.environ.get("PRICING_API_SECRET", "")

def api_key_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key", "") or request.form.get("api_key", "")
        if key != API_SECRET:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

@bp.route("/api/lens-pricing/update", methods=["POST"])
@api_key_required
def api_pricing_update():
    """API endpoint for updating a single price (used by eu.lensbazaar.com dashboard)."""
    data = load_pricing()
    context = request.form.get("context")
    category = request.form.get("category")
    code = request.form.get("code")
    new_price = request.form.get("price")
    new_price_eur = request.form.get("price_eur")
    try:
        new_price = int(new_price) if new_price else None
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid price"}), 400
    try:
        new_price_eur = float(new_price_eur) if new_price_eur else None
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid EUR price"}), 400
    if context in data and category in data[context] and code in data[context][category]:
        if new_price is not None:
            data[context][category][code]["price"] = new_price
        if new_price_eur is not None:
            data[context][category][code]["price_eur"] = new_price_eur
        save_pricing(data)
        return jsonify({"success": True, "context": context, "category": category, "code": code, "price": data[context][category][code].get("price"), "price_eur": data[context][category][code].get("price_eur")})
    return jsonify({"error": "Item not found"}), 404

@bp.route("/api/lens-pricing/bulk", methods=["POST"])
@api_key_required
def api_pricing_bulk_update():
    """API endpoint for bulk price update (used by eu.lensbazaar.com dashboard)."""
    try:
        raw = request.form.get("pricing_json", "")
        data = json.loads(raw)
        save_pricing(data)
        return jsonify({"success": True})
    except (json.JSONDecodeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


