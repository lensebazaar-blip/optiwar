"""Manual DTDC shipments — ad-hoc consignments to addresses that are not an
Optiwar/EU order (stores, opticians, returns to supplier, samples).

Stored in the ops platform DB (not optiwar2): an address book plus a log of
every manual AWB booked. No customer notifications are sent from this path.
"""
import json
import logging
import re

from database import db_cursor
from services import dtdc

log = logging.getLogger(__name__)

DESCRIPTIONS = ("Spectacle Frames", "Contact Lenses", "Spectacle Frames and Contact Lenses")


def init_tables():
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS manual_address_book (
                id INT AUTO_INCREMENT PRIMARY KEY,
                label VARCHAR(100) NOT NULL,
                name VARCHAR(150) NOT NULL,
                phone VARCHAR(20) NOT NULL,
                address_1 VARCHAR(255) NOT NULL,
                address_2 VARCHAR(255) DEFAULT '',
                city VARCHAR(100) NOT NULL,
                state VARCHAR(100) NOT NULL,
                pincode VARCHAR(10) NOT NULL,
                created_by INT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_label (label)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS manual_shipments (
                id INT AUTO_INCREMENT PRIMARY KEY,
                reference VARCHAR(50) NOT NULL,
                address_id INT,
                consignee_name VARCHAR(150) NOT NULL,
                consignee_phone VARCHAR(20) NOT NULL,
                address_1 VARCHAR(255) NOT NULL,
                address_2 VARCHAR(255) DEFAULT '',
                city VARCHAR(100) NOT NULL,
                state VARCHAR(100) NOT NULL,
                pincode VARCHAR(10) NOT NULL,
                courier VARCHAR(20) NOT NULL DEFAULT 'DTDC',
                shipping_mode VARCHAR(10) NOT NULL DEFAULT 'Express',
                payment_mode VARCHAR(10) NOT NULL DEFAULT 'Prepaid',
                declared_value DECIMAL(10,2) DEFAULT 0,
                cod_amount DECIMAL(10,2) DEFAULT 0,
                weight_kg DECIMAL(6,3) DEFAULT 0.25,
                description VARCHAR(250),
                purpose VARCHAR(255),
                tracking_number VARCHAR(50),
                awb_status VARCHAR(20) NOT NULL DEFAULT 'created',
                label_pdf_path VARCHAR(255),
                courier_response JSON,
                created_by INT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_tracking (tracking_number)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)


def validate_address(a: dict) -> list:
    errs = []
    if not a.get("name", "").strip():
        errs.append("Consignee name is required")
    phone = re.sub(r"\D", "", a.get("phone", ""))
    if len(phone) < 10:
        errs.append("Phone must have at least 10 digits")
    if not a.get("address_1", "").strip():
        errs.append("Address line 1 is required")
    if not a.get("city", "").strip():
        errs.append("City is required")
    if not a.get("state", "").strip():
        errs.append("State is required")
    if not re.fullmatch(r"\d{6}", a.get("pincode", "").strip()):
        errs.append("PIN code must be 6 digits")
    return errs


def list_addresses() -> list:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM manual_address_book ORDER BY label")
        return cur.fetchall()


def get_address(address_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM manual_address_book WHERE id = %s", (address_id,))
        return cur.fetchone()


def save_address(a: dict, user_id: int) -> int:
    """Insert or update (by label) an address-book entry; returns its id."""
    label = a["label"].strip()[:100]
    with db_cursor() as cur:
        cur.execute("SELECT id FROM manual_address_book WHERE label = %s", (label,))
        row = cur.fetchone()
        vals = (a["name"].strip(), a["phone"].strip(), a["address_1"].strip(),
                (a.get("address_2") or "").strip(), a["city"].strip(), a["state"].strip(),
                a["pincode"].strip())
        if row:
            cur.execute("""UPDATE manual_address_book SET name=%s, phone=%s, address_1=%s,
                           address_2=%s, city=%s, state=%s, pincode=%s WHERE id=%s""",
                        vals + (row["id"],))
            return row["id"]
        cur.execute("""INSERT INTO manual_address_book
                       (label, name, phone, address_1, address_2, city, state, pincode, created_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""", (label,) + vals + (user_id,))
        return cur.lastrowid


def delete_address(address_id: int):
    with db_cursor() as cur:
        cur.execute("DELETE FROM manual_address_book WHERE id = %s", (address_id,))


def list_shipments(limit: int = 50) -> list:
    with db_cursor() as cur:
        cur.execute("""SELECT s.*, u.username AS created_by_name
                       FROM manual_shipments s LEFT JOIN platform_users u ON u.id = s.created_by
                       ORDER BY s.id DESC LIMIT %s""", (limit,))
        return cur.fetchall()


def get_shipment(shipment_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM manual_shipments WHERE id = %s", (shipment_id,))
        return cur.fetchone()


def book(a: dict, opts: dict, user_id: int, address_id=None) -> dict:
    """Book a DTDC consignment for a manual address and record it.
    Returns the manual_shipments row. Raises dtdc.DtdcError on courier failure."""
    with db_cursor() as cur:
        cur.execute("""INSERT INTO manual_shipments
            (reference, address_id, consignee_name, consignee_phone, address_1, address_2, city,
             state, pincode, shipping_mode, payment_mode, declared_value, cod_amount, weight_kg,
             description, purpose, awb_status, created_by)
            VALUES ('', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)""",
            (address_id, a["name"].strip(), a["phone"].strip(), a["address_1"].strip(),
             (a.get("address_2") or "").strip(), a["city"].strip(), a["state"].strip(),
             a["pincode"].strip(), opts["shipping_mode"], opts["payment_mode"],
             opts["declared_value"], opts["cod_amount"], opts["weight_kg"],
             opts["description"], opts.get("purpose", ""), user_id))
        sid = cur.lastrowid
        reference = f"MAN-{sid:05d}"
        cur.execute("UPDATE manual_shipments SET reference = %s WHERE id = %s", (reference, sid))

    order = {
        "order_id": reference,
        "firstname": a["name"].strip(), "lastname": "",
        "telephone": a["phone"].strip(),
        "shipping_address_1": a["address_1"].strip(),
        "shipping_address_2": (a.get("address_2") or "").strip(),
        "shipping_city": a["city"].strip(), "shipping_zone": a["state"].strip(),
        "shipping_postcode": a["pincode"].strip(),
        "total_inr": opts["declared_value"], "products": [],
    }
    params = {
        "reference": reference,
        "shipping_mode": opts["shipping_mode"], "payment_mode": opts["payment_mode"],
        "weight_kg": opts["weight_kg"], "total_amount": opts["declared_value"],
        "cod_amount": opts["cod_amount"], "products_desc": opts["description"], "qty": 1,
    }
    try:
        resp = dtdc.create_consignment(order, params)
    except dtdc.DtdcError as e:
        with db_cursor() as cur:
            cur.execute("""UPDATE manual_shipments SET awb_status='failed', courier_response=%s
                           WHERE id=%s""", (json.dumps({"error": str(e), **(e.details or {})})[:60000], sid))
        raise
    cn = dtdc.extract_cn(resp)
    label = dtdc.save_label_pdf(cn, reference) if cn else ""
    with db_cursor() as cur:
        cur.execute("""UPDATE manual_shipments SET tracking_number=%s, awb_status='created',
                       label_pdf_path=%s, courier_response=%s WHERE id=%s""",
                    (cn, label or None, json.dumps(resp)[:60000], sid))
    log.info("Manual DTDC shipment %s booked: AWB %s -> %s %s", reference, cn, a["name"], a["pincode"])
    return get_shipment(sid)


def cancel(shipment_id: int) -> dict:
    s = get_shipment(shipment_id)
    if not s or not s.get("tracking_number"):
        raise dtdc.DtdcError("No AWB to cancel")
    resp = dtdc.cancel_consignment(s["tracking_number"])
    with db_cursor() as cur:
        cur.execute("UPDATE manual_shipments SET awb_status='cancelled' WHERE id=%s", (shipment_id,))
    return resp
