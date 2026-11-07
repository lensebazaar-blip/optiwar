"""Delivery options at checkout (owner, 7 Nov 2026).

Three choices on the Pay step, India storefront only:

* **Standard**: free, promised order date + 5 days.
* **Expedite**: Rs 300, promised order date + 1 day.
* **24 hours (Delhi NCR)**: Rs 1000, order date + 1 day, offered only when the
  delivery address is in Delhi, Noida, Gurgaon (Gurugram) or Ghaziabad, judged by
  the pin code first and the state name second.

An order that carries an AI Glasses pre-order (dispatch weeks out) keeps the
standard option only; the global storefront keeps its own shipping terms.

The fee is part of the grand total the gateway charges, so
``paid_orders.order_amount_minor`` adds it from ``order_shipping`` (an additive
table keyed by order id) and the success page and the Ops note read the same
row. Stdlib only at import; the deploy tool reads ``TABLES`` plainly.
"""
import datetime

STANDARD = "standard"
EXPEDITE = "expedite"
NCR24 = "ncr24"

OPTIONS = (
    {"key": STANDARD, "label": "Standard delivery", "fee": 0, "days": 5,
     "blurb": "Free. Dispatched and delivered within 5 days of your order.", "ncr_only": False},
    {"key": EXPEDITE, "label": "Expedite", "fee": 300, "days": 1,
     "blurb": "Rs 300. Your order goes out first; promised by the next day.", "ncr_only": False},
    {"key": NCR24, "label": "24-hour delivery (Delhi NCR)", "fee": 1000, "days": 1,
     "blurb": "Rs 1000. Delhi, Noida, Gurgaon and Ghaziabad addresses only.", "ncr_only": True},
)
BY_KEY = {o["key"]: o for o in OPTIONS}

# Pin-code prefixes: Delhi 110xxx, Gurgaon 122xxx, Ghaziabad 2010xx / 2011xx, Noida 2013xx.
NCR_ZIP_PREFIXES = ("110", "122", "2010", "2011", "2013")
NCR_STATES = ("delhi", "new delhi", "nct of delhi", "delhi ncr")
NCR_CITY_WORDS = ("delhi", "noida", "gurgaon", "gurugram", "ghaziabad")

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_shipping (
    order_id VARCHAR(18) NOT NULL PRIMARY KEY,
    option_key VARCHAR(16) NOT NULL,
    fee DECIMAL(10,2) NOT NULL DEFAULT 0,
    promised_date DATE NULL,
    state VARCHAR(80) NULL,
    zipcode VARCHAR(16) NULL,
    created_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
TABLES = (("order_shipping", SCHEMA),)


def _digits(zipcode):
    return "".join(ch for ch in str(zipcode or "") if ch.isdigit())


def is_ncr(state, zipcode, address=""):
    """Delhi NCR by pin code first, then by state, then by a city word in the address."""
    z = _digits(zipcode)
    if z and any(z.startswith(p) for p in NCR_ZIP_PREFIXES):
        return True
    if z and len(z) == 6:
        return False                      # a full pin outside NCR settles it
    s = (state or "").strip().lower()
    if s in NCR_STATES:
        return True
    a = (address or "").lower()
    return any(w in a for w in NCR_CITY_WORDS)


def available(state, zipcode, is_india=True, preorder=False, address=""):
    """The options this address may choose, standard first."""
    if not is_india or preorder:
        return [BY_KEY[STANDARD]]
    out = [BY_KEY[STANDARD], BY_KEY[EXPEDITE]]
    if is_ncr(state, zipcode, address):
        out.append(BY_KEY[NCR24])
    return out


def choose(key, state, zipcode, is_india=True, preorder=False, address=""):
    """The option for a posted key, or standard when the key is unknown or not
    offered to this address (a page that showed it cannot be trusted alone)."""
    key = (key or "").strip().lower()
    for option in available(state, zipcode, is_india, preorder, address):
        if option["key"] == key:
            return option
    return BY_KEY[STANDARD]


def promised_date(order_date, key):
    """Order date + the option's days, pushed past a Sunday."""
    day = order_date.date() if isinstance(order_date, datetime.datetime) else order_date
    day = day + datetime.timedelta(days=BY_KEY.get(key, BY_KEY[STANDARD])["days"])
    if day.weekday() == 6:
        day += datetime.timedelta(days=1)
    return day


def record(cursor, order_id, option, promised, state=None, zipcode=None, now=None):
    """Store the choice for an order; False (never raises) when the table is missing."""
    try:
        cursor.execute(
            "INSERT INTO order_shipping (order_id, option_key, fee, promised_date, state, zipcode, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE option_key = VALUES(option_key), "
            "fee = VALUES(fee), promised_date = VALUES(promised_date), state = VALUES(state), zipcode = VALUES(zipcode)",
            (order_id, option["key"], option["fee"], promised, state, zipcode, now or datetime.datetime.now()))
        return True
    except Exception:  # noqa: BLE001
        return False


def read(cursor, order_id):
    try:
        cursor.execute("SELECT option_key, fee, promised_date FROM order_shipping WHERE order_id = %s", (order_id,))
        return cursor.fetchone() or None
    except Exception:  # noqa: BLE001
        return None


def fee_for(cursor, order_id):
    """The shipping fee on an order, 0.0 when none or the table is missing."""
    row = read(cursor, order_id)
    try:
        return float((row or {}).get("fee") or 0)
    except (TypeError, ValueError):
        return 0.0


def note(row):
    """One line for the order page, the mail and Ops; '' for standard or none."""
    if not row or row.get("option_key") in (None, "", STANDARD):
        return ""
    option = BY_KEY.get(row.get("option_key"))
    if not option:
        return ""
    when = row.get("promised_date")
    when = when.strftime("%A, %d %B %Y") if hasattr(when, "strftime") else str(when or "")
    return "%s (Rs %d paid)%s." % (option["label"].upper(), int(float(row.get("fee") or option["fee"])),
                                   (": promised by " + when) if when else "")


def js_rules():
    """What the checkout page needs to show the right options as the address changes."""
    return {"zip_prefixes": list(NCR_ZIP_PREFIXES), "states": list(NCR_STATES), "city_words": list(NCR_CITY_WORDS),
            "options": [{"key": o["key"], "label": o["label"], "fee": o["fee"], "days": o["days"],
                         "blurb": o["blurb"], "ncr_only": o["ncr_only"]} for o in OPTIONS]}
