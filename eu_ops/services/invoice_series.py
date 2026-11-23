"""Per-site GST invoice number series."""
import re
from datetime import date, datetime


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INVOICE_NO_RE = re.compile(r"^[A-Za-z0-9/-]+$")


def fy_label(d) -> str:
    if isinstance(d, datetime):
        d = d.date()
    if not isinstance(d, date):
        raise TypeError("d must be a date or datetime")
    start_year = d.year if d.month >= 4 else d.year - 1
    return f"{start_year % 100:02d}-{(start_year + 1) % 100:02d}"


def format_no(prefix, fy, n) -> str:
    invoice_no = f"{prefix}/{fy}/{n}"
    assert len(invoice_no) <= 16
    assert _INVOICE_NO_RE.fullmatch(invoice_no)
    return invoice_no


def legacy_eu(order: dict) -> str:
    date_added = order.get("date_added")
    if isinstance(date_added, str):
        try:
            date_added = datetime.fromisoformat(date_added[:19])
        except ValueError:
            date_added = None
    if isinstance(date_added, (date, datetime)):
        year = date_added.year
    else:
        year = date.today().year
    return f"ELB/{year}/{order['order_id']}"


def legacy_ow(order_id) -> str:
    return f"OW/{order_id}"


def _validate_table_name(name):
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"Invalid table name: {name!r}")


def _first(row, key):
    return row[key] if isinstance(row, dict) else row[0]


def allot_and_issue(conn, series_table, meta_table, meta_insert_sql,
                    meta_params, order_id, prefix, fy):
    """Allocate the next number and insert the metadata row in one transaction.

    Returns the number, or None when the order already has an invoice (an
    issued number, or a legacy row without one, is never re-allotted). Every
    issuer locks the FY's series row first, so issues are serialised and the
    numbers stay consecutive with no gaps."""
    _validate_table_name(series_table)
    _validate_table_name(meta_table)
    try:
        conn.autocommit(True)
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM `{meta_table}` WHERE order_id=%s",
                        (order_id,))
            if cur.fetchone():
                return None
            cur.execute(
                f"INSERT IGNORE INTO `{series_table}` (fy, next_no) VALUES (%s, 1)",
                (fy,))
        conn.autocommit(False)
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT next_no FROM `{series_table}` WHERE fy=%s FOR UPDATE",
                (fy,))
            series_row = cur.fetchone()
            if not series_row:
                raise RuntimeError(f"Missing invoice series row for FY {fy}")
            next_no = _first(series_row, "next_no")
            cur.execute(f"SELECT 1 FROM `{meta_table}` WHERE order_id=%s",
                        (order_id,))
            if cur.fetchone():
                conn.rollback()
                return None
            invoice_no = format_no(prefix, fy, next_no)
            cur.execute(
                f"UPDATE `{series_table}` SET next_no=next_no+1 WHERE fy=%s",
                (fy,))
            cur.execute(meta_insert_sql, tuple(meta_params) + (invoice_no,))
        conn.commit()
        return invoice_no
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
