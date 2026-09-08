"""The contact-lens prescription an order was placed with, kept as data.

A lens line in the cart carries its per-eye parameters (``lens_order.cart_item``)
but until now reached the order tables with ``rx_id = NULL``: the powers lived
only in the session and a paid order arrived at Ops without them. This module
is the one place a lens prescription is written and read back.

Two rows are written per lens line, in the caller's transaction:

* ``rx_collector`` — the legacy row every existing order surface already joins
  on (``orders.rx_id``: success page, profile history, admin dashboards,
  mail). Its ``right_eye``/``left_eye`` strings keep the shape the lens branch
  of those templates parses, ``pwr/cyl/qty/color``.
* ``contact_lens_prescriptions`` — the canonical row: typed per-eye fields,
  ``rx_type``, ``source`` (provenance), the customer who owns it, the order it
  is a snapshot of, and the retention date. This is what Saved prescriptions,
  Ops and the daily report read; nothing downstream parses a string.

The snapshot is immutable by convention: a row with an ``order_id`` is never
updated by the storefront. A later reuse (Saved) writes a new row with
``source = SAVED_REUSED`` and ``reused_from`` pointing here.
"""
import datetime

try:
    from . import lens_order
except ImportError:  # run as a plain module (tests, deploy tool, scripts)
    import lens_order

RX_TYPE_CONTACT_LENS = "contact_lens"

SOURCE_MANUAL = "MANUAL"
SOURCE_UPLOADED_CONFIRMED = "UPLOADED_CONFIRMED"
SOURCE_SAVED_REUSED = "SAVED_REUSED"
SOURCE_AI_ASSISTED_CONFIRMED = "AI_ASSISTED_CONFIRMED"
SOURCE_ORDER_HISTORY = "ORDER_HISTORY"
SOURCES = (SOURCE_MANUAL, SOURCE_UPLOADED_CONFIRMED, SOURCE_SAVED_REUSED,
           SOURCE_AI_ASSISTED_CONFIRMED, SOURCE_ORDER_HISTORY)

# Originals and their extracted values are kept this long after the order;
# the daily report counts what has passed the date, a job deletes it.
RETENTION_MONTHS = 24

EYE_FIELDS = ("sph", "cyl", "axis", "add_power", "base_curve", "diameter",
              "color_code", "variant_id", "boxes")

SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_prescriptions (
    cl_rx_id          INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    rx_id             INT NULL,
    customer_id       INT NULL,
    order_id          VARCHAR(64) NULL,
    product_id        INT NOT NULL,
    rx_type           VARCHAR(16) NOT NULL DEFAULT 'contact_lens',
    source            VARCHAR(32) NOT NULL,
    document_id       INT UNSIGNED NULL,
    reused_from       INT UNSIGNED NULL,
    right_sph         DECIMAL(5,2) NULL,
    right_cyl         DECIMAL(5,2) NULL,
    right_axis        SMALLINT UNSIGNED NULL,
    right_add_power   DECIMAL(5,2) NULL,
    right_base_curve  DECIMAL(4,2) NULL,
    right_diameter    DECIMAL(4,2) NULL,
    right_color_code  VARCHAR(40) NULL,
    right_variant_id  INT NULL,
    right_boxes       SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    left_sph          DECIMAL(5,2) NULL,
    left_cyl          DECIMAL(5,2) NULL,
    left_axis         SMALLINT UNSIGNED NULL,
    left_add_power    DECIMAL(5,2) NULL,
    left_base_curve   DECIMAL(4,2) NULL,
    left_diameter     DECIMAL(4,2) NULL,
    left_color_code   VARCHAR(40) NULL,
    left_variant_id   INT NULL,
    left_boxes        SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    site              VARCHAR(32) NULL,
    created_at        DATETIME NOT NULL,
    retain_until      DATE NOT NULL,
    KEY ix_clrx_customer (customer_id, created_at),
    KEY ix_clrx_order (order_id),
    KEY ix_clrx_created (created_at),
    KEY ix_clrx_retain (retain_until),
    UNIQUE KEY uq_clrx_rx (rx_id),
    UNIQUE KEY uq_clrx_line (order_id, product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLE = ("contact_lens_prescriptions", SCHEMA)


def _dec(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def is_lens_line(item):
    return (item or {}).get("vertical") == "CONTACT_LENS" or \
        (item or {}).get("product_category") == "Contact Lenses"


def eyes_from_item(item):
    """The per-eye values of a ``lens_order.cart_item`` as typed fields."""
    out = {}
    for eye in lens_order.EYES:
        boxes = _int(item.get("%s_qty" % eye)) or 0
        out[eye] = {
            "sph": _dec(item.get("%s_pwr" % eye)) if boxes else None,
            "cyl": _dec(item.get("%s_cyl" % eye)) if boxes else None,
            "axis": _int(item.get("%s_axis" % eye)) if boxes else None,
            "add_power": _dec(item.get("%s_add" % eye)) if boxes else None,
            "base_curve": _dec(item.get("%s_bc" % eye)) if boxes else None,
            "diameter": _dec(item.get("%s_dia" % eye)) if boxes else None,
            "color_code": (item.get("%s_lens_color" % eye) or None)
            if boxes else None,
            "variant_id": _int(item.get("%s_variant_id" % eye))
            if boxes else None,
            "boxes": boxes,
        }
    return out


def legacy_eye_string(eye_values):
    """``pwr/cyl/qty/color`` — what success.html/profile.html parse for a lens.

    An eye with no boxes is the sentinel the templates already treat as absent.
    """
    if not eye_values or not eye_values.get("boxes"):
        return "No RX selected"

    def dec(v):
        return "" if v is None else "%+.2f" % v

    # Positions 0-3 are the legacy layout; axis/add/BC follow so a toric or
    # multifocal line is not truncated by the compatibility string.
    axis = eye_values.get("axis")
    return "%s/%s/%d/%s/%s/%s/%s" % (
        dec(eye_values.get("sph")), dec(eye_values.get("cyl")),
        eye_values["boxes"], eye_values.get("color_code") or "",
        "" if axis is None else str(int(axis)),
        dec(eye_values.get("add_power")),
        "" if eye_values.get("base_curve") is None
        else "%.2f" % eye_values["base_curve"])


def retain_until(now):
    month = now.month - 1 + RETENTION_MONTHS
    year = now.year + month // 12
    month = month % 12 + 1
    day = min(now.day, 28)
    return datetime.date(year, month, day)


def record(cursor, item, customer_id, order_id, site, source=SOURCE_MANUAL,
           document_id=None, reused_from=None, now=None):
    """Write the two rows for one lens cart line; return the ``rx_id``.

    Runs on the caller's cursor, inside the caller's transaction, so the order
    line and its prescription commit together or not at all. Idempotent per
    order line: a snapshot already recorded for ``(order_id, product_id)`` is
    returned rather than duplicated, which is what a retried checkout hits.
    """
    if source not in SOURCES:
        raise ValueError("unknown prescription source %r" % (source,))
    now = now or datetime.datetime.now()
    product_id = int(item["product_id"])
    lock_name = _lock_name(order_id, product_id)
    _acquire(cursor, lock_name)
    try:
        existing = _existing(cursor, order_id, product_id, lock=True)
        if existing is not None:
            return existing
        return _insert(cursor, item, product_id, customer_id, order_id, site,
                       source, document_id, reused_from, now)
    finally:
        _release(cursor, lock_name)


LOCK_WAIT_SECONDS = 10


def _lock_name(order_id, product_id):
    return "clrx:%s:%d" % (order_id or "-", product_id) if order_id else None


def _acquire(cursor, name):
    """Serialise concurrent writers of one order line on an advisory lock.

    Several checkout submits of the same order racing on the unique key would
    otherwise deadlock each other on the duplicate-key shared locks (error
    1213) — the lock makes the losers wait their turn and then find the row.
    """
    if not name:
        return
    cursor.execute("SELECT GET_LOCK(%s, %s) AS l", (name, LOCK_WAIT_SECONDS))
    row = cursor.fetchone()
    got = (row["l"] if isinstance(row, dict) else row[0]) if row else None
    if got == 0:
        raise RuntimeError("prescription lock %s not acquired in %ds"
                           % (name, LOCK_WAIT_SECONDS))


def _release(cursor, name):
    if name:
        cursor.execute("SELECT RELEASE_LOCK(%s)", (name,))
        cursor.fetchone()


def _insert(cursor, item, product_id, customer_id, order_id, site, source,
            document_id, reused_from, now):
    eyes = eyes_from_item(item)
    cursor.execute(
        "INSERT INTO rx_collector (recommendations, recommendation_price, "
        "right_eye, left_eye, product_id) VALUES (%s, %s, %s, %s, %s)",
        (item.get("product_name"), _int(item.get("product_special_price")),
         legacy_eye_string(eyes["right"]), legacy_eye_string(eyes["left"]),
         int(item["product_id"])))
    rx_id = cursor.lastrowid
    cols = ["rx_id", "customer_id", "order_id", "product_id", "rx_type",
            "source", "document_id", "reused_from", "site", "created_at",
            "retain_until"]
    vals = [rx_id, customer_id, order_id, int(item["product_id"]),
            RX_TYPE_CONTACT_LENS, source, document_id, reused_from, site,
            now.strftime("%Y-%m-%d %H:%M:%S"), retain_until(now)]
    for eye in lens_order.EYES:
        for field in EYE_FIELDS:
            cols.append("%s_%s" % (eye, field))
            vals.append(eyes[eye][field])
    try:
        cursor.execute(
            "INSERT INTO contact_lens_prescriptions (%s) VALUES (%s)"
            % (", ".join(cols), ", ".join(["%s"] * len(vals))), tuple(vals))
    except Exception as exc:  # noqa: BLE001 - narrowed below
        if not _is_duplicate(exc):
            raise
        # Lost a race on uq_clrx_line despite the advisory lock (a writer on
        # a connection that bypassed record()): the committed row is the
        # snapshot; ours is dropped and the legacy row it would own with it.
        cursor.execute("DELETE FROM rx_collector WHERE rx_id = %s", (rx_id,))
        existing = _existing(cursor, order_id, product_id, lock=True)
        if existing is None:
            raise
        return existing
    return rx_id


def _existing(cursor, order_id, product_id, lock=False):
    if not order_id:
        return None
    cursor.execute(
        "SELECT rx_id FROM contact_lens_prescriptions "
        "WHERE order_id = %s AND product_id = %s" + (" FOR UPDATE" if lock else ""),
        (order_id, product_id))
    row = cursor.fetchone()
    if not row:
        return None
    return row["rx_id"] if isinstance(row, dict) else row[0]


def _is_duplicate(exc):
    """MySQL/MariaDB error 1062 (ER_DUP_ENTRY), from either driver."""
    args = exc.args if hasattr(exc, "args") else ()
    return bool(args) and args[0] == 1062


SELECT_COLS = (
    "cl_rx_id, rx_id, customer_id, order_id, product_id, rx_type, source, "
    "document_id, reused_from, site, created_at, retain_until, "
    + ", ".join("%s_%s" % (eye, f) for eye in lens_order.EYES
                for f in EYE_FIELDS))


def _rows(cursor):
    rows = cursor.fetchall() or ()
    if rows and not isinstance(rows[0], dict):
        names = SELECT_COLS.split(", ")
        rows = [dict(zip(names, r)) for r in rows]
    return list(rows)


def for_order(cursor, order_id):
    """Every lens prescription snapshot on an order (one per lens line)."""
    cursor.execute(
        "SELECT %s FROM contact_lens_prescriptions WHERE order_id = %s "
        "ORDER BY cl_rx_id" % (SELECT_COLS, "%s"), (order_id,))
    return _rows(cursor)


def for_customer(cursor, customer_id, limit=20):
    """A customer's own contact-lens prescriptions, newest first.

    The owner is the authenticated ``customer_id`` the caller took from the
    session; a row is never looked up by an id the client sent.
    """
    if not customer_id:
        return []
    cursor.execute(
        "SELECT %s FROM contact_lens_prescriptions WHERE customer_id = %s "
        "AND rx_type = %s ORDER BY created_at DESC, cl_rx_id DESC LIMIT %s"
        % (SELECT_COLS, "%s", "%s", "%s"),
        (int(customer_id), RX_TYPE_CONTACT_LENS, int(limit)))
    return _rows(cursor)


def describe_row(row):
    """One line per ordered eye, for an Ops view or an email — never a log."""
    lines = []
    for eye, label in (("right", "RIGHT (OD)"), ("left", "LEFT (OS)")):
        boxes = row.get("%s_boxes" % eye) or 0
        if not boxes:
            continue
        variant = {
            "sph": row.get("%s_sph" % eye),
            "cyl": row.get("%s_cyl" % eye),
            "axis": row.get("%s_axis" % eye),
            "add_power": row.get("%s_add_power" % eye),
            "base_curve": row.get("%s_base_curve" % eye),
            "color_code": row.get("%s_color_code" % eye),
        }
        lines.append("%s: %s" % (label, lens_order.describe(variant, boxes)))
    return lines
