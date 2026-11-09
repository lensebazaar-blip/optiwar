"""Ops controls: the few numbers Ops may change without a release, and the
indicators that tell them when to.

* **Settings** (``ops_settings``, every change logged in ``ops_settings_log``
  with who and when): the 24-hour delivery price and the photo reader's two
  daily limits. Each is bounded, so a typo cannot set a price of Rs 1.
* **Indicators**: photo reads used today against the limit, clients at or near
  their limit, and flagged uploads waiting for review. The page blinks them.

Routes (``ops._require_ops_auth``: admin session or Bearer ``OPS_API_TOKEN``):

    GET  /ops/controls                         the page
    GET  /api/ops/controls                     settings + indicators as JSON
    POST /api/ops/controls                     {"key": ..., "value": n} or {"key": ..., "delta": n}
    POST /api/ops/controls/flags/<id>/review   a person has reviewed a flagged upload

Any Ops dashboard can drive the JSON routes. Stdlib at import, so the deploy tool
can read ``TABLES`` plainly; Flask and the DB are imported inside ``register``.
"""
import datetime

NCR24_FEE = "ncr24_fee"
RX_PER_IP = "rx_reads_per_ip"
RX_PER_DAY = "rx_reads_per_day"

SETTINGS = {
    NCR24_FEE: {"label": "24-hour delivery price (Rs, 18% GST inclusive)", "default": 1000, "min": 100, "max": 10000, "step": 50},
    RX_PER_IP: {"label": "Photo reader: reads per customer IP per day", "default": 20, "min": 1, "max": 500, "step": 5},
    RX_PER_DAY: {"label": "Photo reader: reads per day, all customers", "default": 300, "min": 10, "max": 20000, "step": 50},
}
NEAR_LIMIT = 0.8

SCHEMA = """
CREATE TABLE IF NOT EXISTS ops_settings (
    setting_key VARCHAR(40) NOT NULL PRIMARY KEY,
    setting_value INT NOT NULL,
    updated_by VARCHAR(191) NOT NULL,
    updated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS ops_settings_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    setting_key VARCHAR(40) NOT NULL,
    old_value INT NULL,
    new_value INT NOT NULL,
    changed_by VARCHAR(191) NOT NULL,
    changed_at DATETIME NOT NULL,
    KEY ix_ops_settings_log (setting_key, changed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
TABLES = (("ops_settings", SCHEMA), ("ops_settings_log", LOG_SCHEMA))


def get(cursor, key):
    """The value in force, or the default when unset or unreadable."""
    default = SETTINGS[key]["default"]
    try:
        cursor.execute("SELECT setting_value FROM ops_settings WHERE setting_key = %s", (key,))
        row = cursor.fetchone()
        return int(row["setting_value"]) if row else default
    except Exception:  # noqa: BLE001
        return default


def validate(key, value):
    """``(int_value, error)``."""
    spec = SETTINGS.get(key)
    if not spec:
        return None, "Unknown setting."
    if isinstance(value, bool):
        return None, "Enter a whole number."
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None, "Enter a whole number."
    if str(value).strip() != str(number) and not isinstance(value, int):
        return None, "Enter a whole number."
    if number < spec["min"] or number > spec["max"]:
        return None, "%s must be between %d and %d." % (spec["label"], spec["min"], spec["max"])
    return number, ""


def set_value(cursor, key, value, who, now=None):
    now = now or datetime.datetime.now()
    old = get(cursor, key)
    cursor.execute(
        "INSERT INTO ops_settings (setting_key, setting_value, updated_by, updated_at) VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value), updated_by = VALUES(updated_by), "
        "updated_at = VALUES(updated_at)", (key, value, who, now))
    cursor.execute("INSERT INTO ops_settings_log (setting_key, old_value, new_value, changed_by, changed_at) "
                   "VALUES (%s, %s, %s, %s, %s)", (key, old, value, who, now))
    return old


def indicators(cursor, per_ip, per_day, today=None):
    """Today's photo reader usage and whether each indicator should blink."""
    today = today or datetime.date.today()
    out = {"reads_today": 0, "per_day": per_day, "per_ip": per_ip, "at_limit": 0, "near_limit": 0, "open_flags": 0}
    try:
        cursor.execute("SELECT client_ip, read_count FROM rx_photo_quota WHERE day = %s AND client_ip NOT LIKE 'min:%%'", (today,))
        for row in cursor.fetchall():
            count = int(row["read_count"])
            if row["client_ip"] == "*":
                out["reads_today"] = count
            elif count >= per_ip:
                out["at_limit"] += 1
            elif count >= per_ip * NEAR_LIMIT:
                out["near_limit"] += 1
        cursor.execute("SELECT COUNT(*) AS n FROM rx_photo_flags WHERE reviewed_at IS NULL")
        out["open_flags"] = int((cursor.fetchone() or {}).get("n") or 0)
    except Exception:  # noqa: BLE001 - tables appear with the first read
        pass
    out["day_blink"] = out["reads_today"] >= per_day * NEAR_LIMIT
    out["ip_blink"] = out["at_limit"] > 0
    out["flag_blink"] = out["open_flags"] > 0
    return out


def open_flags(cursor, limit=50):
    try:
        cursor.execute("SELECT id, created_at, category, sha256, client_ip, customer_id FROM rx_photo_flags "
                       "WHERE reviewed_at IS NULL ORDER BY id DESC LIMIT %s", (limit,))
        return list(cursor.fetchall())
    except Exception:  # noqa: BLE001
        return []


def snapshot(cursor):
    values = {key: get(cursor, key) for key in SETTINGS}
    return {"settings": [dict(SETTINGS[k], key=k, value=values[k]) for k in SETTINGS],
            "indicators": indicators(cursor, values[RX_PER_IP], values[RX_PER_DAY]), "flags": open_flags(cursor)}


def register(bp):
    from flask import current_app, jsonify, render_template, request, session  # noqa: PLC0415

    def _auth():
        from .ops import _require_ops_auth  # noqa: PLC0415
        return _require_ops_auth()

    def _operator():
        return (session.get("user_email") or request.headers.get("X-Ops-Operator") or "").strip()[:191] or None

    def _db():
        from .db import get_db  # noqa: PLC0415
        db = get_db()
        cursor = db.cursor()
        for _name, ddl in TABLES:
            cursor.execute(ddl)
        return db, cursor

    def _json_ready(snap):
        for flag in snap["flags"]:
            if hasattr(flag.get("created_at"), "isoformat"):
                flag["created_at"] = flag["created_at"].isoformat()
        return snap

    @bp.route("/ops/controls", methods=["GET"])
    def ops_controls_page():
        if not _auth():
            return jsonify({"error": "Unauthorized"}), 401
        _db_, cursor = _db()
        resp = current_app.make_response(render_template("ops_controls.html", operator=_operator() or "", **snapshot(cursor)))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @bp.route("/api/ops/controls", methods=["GET"])
    def ops_controls_get():
        if not _auth():
            return jsonify({"error": "Unauthorized"}), 401
        _db_, cursor = _db()
        return jsonify(dict(_json_ready(snapshot(cursor)), ok=True))

    @bp.route("/api/ops/controls", methods=["POST"])
    def ops_controls_set():
        if not _auth():
            return jsonify({"error": "Unauthorized"}), 401
        who = _operator()
        if not who:
            return jsonify({"ok": False, "error": "operator_required", "message": "Name the person making this change."}), 400
        body = request.get_json(silent=True) or {}
        key = body.get("key")
        if key not in SETTINGS:
            return jsonify({"ok": False, "error": "unknown_setting", "message": "Unknown setting."}), 400
        db, cursor = _db()
        target = body.get("value")
        if target is None and body.get("delta") is not None:
            try:
                target = get(cursor, key) + int(body["delta"])
            except (TypeError, ValueError):
                target = "x"
        value, error = validate(key, target)
        if error:
            return jsonify({"ok": False, "error": "invalid_value", "message": error}), 400
        old = set_value(cursor, key, value, who)
        db.commit()
        current_app.logger.info("ACTIVITY:OPS_SETTING key:%s old:%s new:%s by:%s", key, old, value, who)
        return jsonify({"ok": True, "key": key, "old": old, "value": value})

    @bp.route("/api/ops/controls/flags/<int:flag_id>/review", methods=["POST"])
    def ops_controls_review_flag(flag_id):
        if not _auth():
            return jsonify({"error": "Unauthorized"}), 401
        who = _operator()
        if not who:
            return jsonify({"ok": False, "error": "operator_required", "message": "Name the person reviewing this."}), 400
        db, cursor = _db()
        cursor.execute("UPDATE rx_photo_flags SET reviewed_at = %s, reviewed_by = %s WHERE id = %s AND reviewed_at IS NULL",
                       (datetime.datetime.now(), who, flag_id))
        db.commit()
        return jsonify({"ok": True, "id": flag_id})
