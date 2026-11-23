"""Progressive-fit face-scan choice and follow-up records."""

import datetime
import logging
from urllib.parse import urlsplit


WARNING_VERSION = "pf-warn-v1"
HEADING = "Progressive lenses need a face scan"
BODY = (
    "Your face scan measures the distance between your pupils for near and far "
    "vision, so your progressive lenses are centred correctly. It takes about "
    "a minute with your phone or laptop camera."
)
BTN_SCAN = "Take face scan"
BTN_SKIP = "Continue without face scan"
WARNING = (
    "Without a face scan, progressive lenses are much more likely not to fit "
    "well. The reading or distance zones can feel blurred or hard to find, and "
    "these orders are returned more often. You can still continue, and you can "
    "take the scan later from your account."
)
TICK = "I understand and want to continue without a face scan"
HISTORY_DECLINED = (
    "Customer continued without face scan after progressive-fit warning"
)
HISTORY_LATER_SCAN = (
    "Customer took the face scan after ordering; the earlier decline after "
    "the progressive-fit warning stays on record"
)

SCANNED = "SCANNED"
DECLINED_AFTER_WARNING = "DECLINED_AFTER_WARNING"

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_face_scan_choice (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    order_id VARCHAR(18) NOT NULL UNIQUE,
    customer_id INT NULL,
    choice ENUM('SCANNED','DECLINED_AFTER_WARNING') NOT NULL,
    warning_version VARCHAR(32) NULL,
    warning_text TEXT NULL,
    warned_at DATETIME NULL,
    decided_at DATETIME NULL,
    face_measurement_id INT NULL,
    scanned_at DATETIME NULL,
    previous_choice VARCHAR(32) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY ix_ofsc_customer_choice (customer_id, choice)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
TABLES = (("order_face_scan_choice", SCHEMA),)

_SCHEMA_READY = False
_LOG = logging.getLogger(__name__)


def _lens_rx():
    try:
        from . import lens_rx
    except ImportError:
        import lens_rx
    return lens_rx


def applies(item):
    if not isinstance(item, dict):
        return False
    return (
        str(item.get("recommendations") or "").startswith(
            "Biofocal or Progressive Selection)")
        and "Spectacles Frame" in str(item.get("product_category", ""))
        and not _lens_rx().is_lens_line(item)
    )


def cart_needs_step(cart):
    return any(applies(item) for item in (cart or []))


def ensure_schema(cursor):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    cursor.execute(SCHEMA)
    _SCHEMA_READY = True


def latest_scan_id(cursor, customer_id):
    if not customer_id:
        return None
    cursor.execute(
        "SELECT id FROM face_measurements WHERE customer_id=%s "
        "ORDER BY measured_at DESC, id DESC LIMIT 1",
        (int(customer_id),))
    row = cursor.fetchone()
    if not row:
        return None
    return row.get("id") if isinstance(row, dict) else row[0]


def has_completed_face_scan(cursor, customer_id):
    if not customer_id:
        return False
    cursor.execute(
        "SELECT id FROM face_scans WHERE customer_id=%s AND status=%s LIMIT 1",
        (int(customer_id), "COMPLETED"))
    return cursor.fetchone() is not None


def has_scan(cursor, customer_id):
    return (latest_scan_id(cursor, customer_id) is not None
            or has_completed_face_scan(cursor, customer_id))


def acknowledgement_is_valid(has_scan, face_scan_ack, warned_at):
    return (bool(has_scan)
            or (face_scan_ack == "1" and bool(warned_at)))


def _as_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value
    try:
        return datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _row_value(row, key):
    if isinstance(row, dict):
        return row.get(key)
    return None


def record(cursor, order_id, customer_id, site, cart, scan_id, warned_at,
           accepted_at, scan_available=None):
    if not cart_needs_step(cart):
        return None
    if scan_available is None:
        scan_available = scan_id is not None

    cursor.execute(
        "SELECT choice, warning_version, warning_text, warned_at, decided_at, "
        "previous_choice FROM order_face_scan_choice WHERE order_id=%s",
        (order_id,))
    existing = cursor.fetchone()
    existing_choice = _row_value(existing, "choice")

    if scan_available:
        choice = SCANNED
        previous_choice = (
            _row_value(existing, "previous_choice")
            or (DECLINED_AFTER_WARNING
                if existing_choice == DECLINED_AFTER_WARNING else None))
        warning_version = _row_value(existing, "warning_version")
        warning_text = _row_value(existing, "warning_text")
        stored_warned_at = _as_datetime(_row_value(existing, "warned_at"))
        stored_decided_at = _as_datetime(_row_value(existing, "decided_at"))
        cursor.execute(
            "INSERT INTO order_face_scan_choice "
            "(order_id, customer_id, choice, warning_version, warning_text, "
            "warned_at, decided_at, face_measurement_id, scanned_at, "
            "previous_choice) "
            "VALUES (%s,%s,%s,%s,%s,%s,COALESCE(%s,NOW()),%s,NOW(),%s) "
            "ON DUPLICATE KEY UPDATE customer_id=VALUES(customer_id), "
            "choice=VALUES(choice), warning_version=VALUES(warning_version), "
            "warning_text=VALUES(warning_text), warned_at=VALUES(warned_at), "
            "decided_at=COALESCE(decided_at,VALUES(decided_at)), "
            "face_measurement_id=VALUES(face_measurement_id), "
            "scanned_at=COALESCE(scanned_at,VALUES(scanned_at)), "
            "previous_choice=VALUES(previous_choice), updated_at=NOW()",
            (order_id, customer_id, choice, warning_version, warning_text,
             stored_warned_at, stored_decided_at, scan_id, previous_choice))
        return choice

    choice = DECLINED_AFTER_WARNING
    warning_version = WARNING_VERSION
    warning_text = WARNING
    stored_warned_at = (_as_datetime(_row_value(existing, "warned_at"))
                        or _as_datetime(warned_at))
    stored_decided_at = (_as_datetime(accepted_at)
                         if existing_choice != DECLINED_AFTER_WARNING else None)
    previous_choice = _row_value(existing, "previous_choice")
    cursor.execute(
        "INSERT INTO order_face_scan_choice "
        "(order_id, customer_id, choice, warning_version, warning_text, "
        "warned_at, decided_at, face_measurement_id, scanned_at, "
        "previous_choice) "
        "VALUES (%s,%s,%s,%s,%s,%s,COALESCE(%s,NOW()),NULL,NULL,%s) "
        "ON DUPLICATE KEY UPDATE customer_id=VALUES(customer_id), "
        "choice=VALUES(choice), warning_version=VALUES(warning_version), "
        "warning_text=VALUES(warning_text), "
        "warned_at=COALESCE(warned_at,VALUES(warned_at)), "
        "decided_at=COALESCE(decided_at,VALUES(decided_at)), "
        "face_measurement_id=NULL, scanned_at=NULL, "
        "previous_choice=VALUES(previous_choice), updated_at=NOW()",
        (order_id, customer_id, choice, warning_version, warning_text,
         stored_warned_at, stored_decided_at, previous_choice))
    if existing_choice != DECLINED_AFTER_WARNING:
        cursor.execute(
            "INSERT INTO order_history "
            "(order_history_content, order_id, site_from) VALUES (%s,%s,%s)",
            (HISTORY_DECLINED, order_id, site))
    return choice


def link_later_scan(cursor, customer_id, face_measurement_id):
    try:
        try:
            from . import refunds
        except ImportError:
            import refunds

        shipped = refunds.SHIPPED_STATUSES
        placeholders = ",".join(["%s"] * len(shipped))
        cursor.execute(
            "SELECT DISTINCT c.order_id, o.site_from "
            "FROM order_face_scan_choice c "
            "LEFT JOIN orders o ON o.order_id=c.order_id "
            "WHERE c.customer_id=%s AND c.choice=%s "
            "AND NOT EXISTS (SELECT 1 FROM order_status s "
            "WHERE s.order_id=c.order_id AND s.order_status_name IN ("
            + placeholders + "))",
            (int(customer_id), DECLINED_AFTER_WARNING) + tuple(shipped))
        orders = cursor.fetchall()
        for row in orders:
            cursor.execute(
                "UPDATE order_face_scan_choice SET choice=%s, "
                "previous_choice=%s, scanned_at=NOW(), "
                "face_measurement_id=%s, updated_at=NOW() "
                "WHERE order_id=%s AND customer_id=%s AND choice=%s",
                (SCANNED, DECLINED_AFTER_WARNING, face_measurement_id,
                 row["order_id"], int(customer_id), DECLINED_AFTER_WARNING))
            if getattr(cursor, "rowcount", 1):
                cursor.execute(
                    "INSERT INTO order_history "
                    "(order_history_content, order_id, site_from) "
                    "VALUES (%s,%s,%s)",
                    (HISTORY_LATER_SCAN, row["order_id"], row.get("site_from")))
    except Exception as exc:  # noqa: BLE001 - a scan save must not fail on bookkeeping
        _LOG.warning("Progressive face-scan link failed for customer %s: %s",
                     customer_id, exc)


def safe_return_path(value):
    target = str(value or "")
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    return target


def register(bp):
    from flask import jsonify, request, session

    @bp.route("/checkout/face-scan/event", methods=["POST"])
    def checkout_face_scan_event():
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "login_required"}), 401
        data = request.get_json(silent=True) or request.form
        event = data.get("event")
        if event == "warned":
            if not session.get("pf_warned_at"):
                session["pf_warned_at"] = datetime.datetime.now().isoformat()
        elif event == "accepted":
            if (session.get("pf_warned_at")
                    and not session.get("pf_accepted_at")):
                session["pf_accepted_at"] = datetime.datetime.now().isoformat()
        else:
            return jsonify({"ok": False, "error": "invalid_event"}), 400
        return jsonify({"ok": True})
