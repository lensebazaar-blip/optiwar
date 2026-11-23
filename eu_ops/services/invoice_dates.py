"""Invoice issue dates: chosen once when an invoice is first opened, then frozen."""
from datetime import date, datetime

from database import db_cursor, get_connection
from services import invoice_series

DISPLAY_FMT = "%d-%m-%Y"


def _ensure_eu_table():
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS eu_invoice_meta (
                order_id INT NOT NULL PRIMARY KEY,
                invoice_date DATE NOT NULL,
                date_source ENUM('today','entered') NOT NULL,
                issued_by INT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def get_eu_invoice_date(order_id: int):
    _ensure_eu_table()
    with db_cursor() as cur:
        cur.execute("SELECT invoice_date FROM eu_invoice_meta WHERE order_id=%s", (order_id,))
        row = cur.fetchone()
    return row["invoice_date"] if row else None


def get_eu_invoice(order_id: int):
    _ensure_eu_table()
    with db_cursor() as cur:
        cur.execute(
            "SELECT invoice_date, invoice_no FROM eu_invoice_meta WHERE order_id=%s",
            (order_id,))
        row = cur.fetchone()
    return row if row else None


def issue_eu_invoice_date(order_id: int, issue_date: date, source: str, issued_by=None) -> bool:
    """Store the issue date and series number unless an invoice already exists."""
    _ensure_eu_table()
    invoice_no = invoice_series.allot_and_issue(
        get_connection(), "eu_invoice_series", "eu_invoice_meta",
        """
            INSERT INTO eu_invoice_meta
                (order_id, invoice_date, date_source, issued_by, invoice_no)
            VALUES (%s, %s, %s, %s, %s)
        """,
        (order_id, issue_date, source, issued_by), order_id, "ELB",
        invoice_series.fy_label(issue_date))
    return invoice_no is not None


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    return None


def fmt(value) -> str:
    d = _as_date(value)
    return d.strftime(DISPLAY_FMT) if d else ""


def resolve_choice(choice: str, print_date: str, order_date, today: date = None):
    """Return (issue_date, source, error) for the prompt's submitted choice."""
    today = today or date.today()
    if choice == "today":
        return today, "today", None
    if choice != "entered":
        return None, None, "Choose today's date or enter the print date."
    try:
        d = datetime.strptime((print_date or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None, None, "Enter the print date."
    if d > today:
        return None, None, "The print date cannot be in the future."
    od = _as_date(order_date)
    if od and d < od:
        return None, None, f"The print date cannot be before the order date ({fmt(od)})."
    return d, "entered", None
