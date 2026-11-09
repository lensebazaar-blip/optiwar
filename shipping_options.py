"""Delivery options at checkout (owner, 7 Nov 2026).

Three choices on the Pay step, India storefront only:

* **Standard**: free, promised order date + 5 days.
* **Expedite**: Rs 300, promised order date + 1 day.
* **24 hours (Delhi NCR)**: Rs 1000 (Ops can change it), order date + 1 day.
  Offered only once a full pin code for Delhi, Noida, Gurgaon or Ghaziabad and
  a state that agrees with it are entered; never on Sunday; on Saturday only
  for orders received by 11:45 India time.

An order that carries any AI Glasses line with a dispatch date of its own
(pre-order, weeks out; selling fast, up to a week) keeps the standard option
only, as does any cart mixed with one; the global storefront keeps its own
shipping terms and never gets a row here.

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
     "blurb": "Rs %d. Delivered within 24 hours of your order.", "ncr_only": True},
)
BY_KEY = {o["key"]: o for o in OPTIONS}

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SATURDAY, SUNDAY = 5, 6
SATURDAY_CUTOFF = datetime.time(11, 45)

# Which states go with which pin prefixes. The pin names the city; the state must agree.
_HARYANA = ("haryana", "hr", "gurgaon", "gurugram", "delhi ncr", "ncr")
_UP = ("uttar pradesh", "up", "u.p.", "noida", "greater noida", "ghaziabad", "delhi ncr", "ncr")
NCR_PINS = (("110", ("delhi", "new delhi", "nct of delhi", "delhi ncr", "ncr")),  # Delhi
            ("122", _HARYANA),   # Gurgaon
            ("2010", _UP), ("2011", _UP),  # Ghaziabad
            ("2013", _UP))       # Noida

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


def ensure_schema(cursor):
    """Create the table if it is not there; True when it can be used. The paid
    options are only offered and charged when this is True, so a fee is never
    taken that the payment check (``paid_orders.order_amount_minor``) cannot see."""
    try:
        for _name, ddl in TABLES:
            cursor.execute(ddl)
        cursor.execute("SELECT 1 FROM order_shipping LIMIT 1")
        cursor.fetchall()
        return True
    except Exception:  # noqa: BLE001
        return False


def _digits(zipcode):
    return "".join(ch for ch in str(zipcode or "") if ch.isdigit())


def now_ist():
    """The time in India, as a naive datetime, whatever the server's own zone."""
    return datetime.datetime.now(IST).replace(tzinfo=None)


def is_ncr(state, zipcode):
    """A full 6-digit Delhi NCR pin code and a state that agrees with it."""
    z = _digits(zipcode)
    if len(z) != 6:
        return False
    s = (state or "").strip().lower()
    return any(z.startswith(prefix) and s in states for prefix, states in NCR_PINS)


def ncr24_open(now=None):
    """Not on Sundays; on Saturdays only for orders received by 11:45 India time."""
    now = now or now_ist()
    if now.weekday() == SUNDAY:
        return False
    return now.weekday() != SATURDAY or now.time() <= SATURDAY_CUTOFF


def ncr24_seconds_left(now=None):
    """Seconds until the option next closes (Saturday 11:45); 0 when it is closed."""
    now = now or now_ist()
    if not ncr24_open(now):
        return 0
    days = (SATURDAY - now.weekday()) % 7
    cutoff = datetime.datetime.combine(now.date() + datetime.timedelta(days=days), SATURDAY_CUTOFF)
    return max(0, int((cutoff - now).total_seconds()))


def _with_fee(option, ncr24_fee):
    if option["key"] != NCR24:
        return option
    fee = int(ncr24_fee if ncr24_fee is not None else option["fee"])
    return dict(option, fee=fee, blurb=option["blurb"] % fee)


def available(state, zipcode, is_india=True, preorder=False, now=None, ncr24_fee=None):
    """The options this address may choose at this moment, standard first."""
    if not is_india or preorder:
        return [BY_KEY[STANDARD]]
    out = [BY_KEY[STANDARD], BY_KEY[EXPEDITE]]
    if is_ncr(state, zipcode) and ncr24_open(now):
        out.append(_with_fee(BY_KEY[NCR24], ncr24_fee))
    return out


def choose(key, state, zipcode, is_india=True, preorder=False, now=None, ncr24_fee=None):
    """The option for a posted key, or standard when the key is unknown or not
    offered to this address (a page that showed it cannot be trusted alone)."""
    key = (key or "").strip().lower()
    for option in available(state, zipcode, is_india, preorder, now, ncr24_fee):
        if option["key"] == key:
            return option
    return BY_KEY[STANDARD]


def promised_date(order_date, key):
    """Order date + the option's days, pushed past a Sunday. The 24-hour option
    is not pushed: a Saturday-morning order is committed for Sunday."""
    day = order_date.date() if isinstance(order_date, datetime.datetime) else order_date
    day = day + datetime.timedelta(days=BY_KEY.get(key, BY_KEY[STANDARD])["days"])
    if day.weekday() == SUNDAY and key != NCR24:
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


def js_rules(ncr24_fee=None, now=None):
    """What the checkout page needs to show the right options as the address changes."""
    return {"ncr_pins": [[prefix, list(states)] for prefix, states in NCR_PINS],
            "ncr24_open": ncr24_open(now), "ncr24_seconds_left": ncr24_seconds_left(now),
            "options": [{"key": o["key"], "label": o["label"], "fee": o["fee"], "days": o["days"],
                         "blurb": o["blurb"], "ncr_only": o["ncr_only"]}
                        for o in (_with_fee(x, ncr24_fee) for x in OPTIONS)]}


def fees_for(cursor, order_ids):
    """{order_id: row} for the orders that carry a delivery choice; {} on any failure."""
    ids = [str(i) for i in order_ids if i]
    if not ids:
        return {}
    try:
        cursor.execute("SELECT order_id, option_key, fee, promised_date FROM order_shipping "
                       "WHERE order_id IN (%s)" % ",".join(["%s"] * len(ids)), ids)
        return {str(r["order_id"]): r for r in cursor.fetchall()}
    except Exception:  # noqa: BLE001
        return {}


def attach_fees(orders, rows):
    """Add the paid delivery option to each order dict (My Orders): the fee joins
    grand_total and the label / promise are shown on their own line."""
    for order in orders or []:
        row = (rows or {}).get(str(order.get("order_id")))
        fee = 0.0
        try:
            fee = float((row or {}).get("fee") or 0)
        except (TypeError, ValueError):
            fee = 0.0
        order["shipping_fee"] = fee
        order["shipping_label"] = BY_KEY.get((row or {}).get("option_key"), {}).get("label", "") if fee else ""
        order["shipping_promised"] = (row or {}).get("promised_date") if fee else None
        if fee:
            order["grand_total"] = (order.get("grand_total") or 0) + fee
    return orders


def mail_cart(cart, row, dispatch_note=""):
    """The cart for the order confirmation mail: a copy with a "Delivery" line for
    a paid option (so the mail's total is the charged total) and a zero-value
    line carrying the dispatch note. The stored cart is never changed, and no
    mail template needs to know about delivery options."""
    out = [dict(item) for item in (cart or [])]

    # Production's mail prints detail text only for lens and frame categories, so
    # everything these lines have to say goes in the name, which it always prints.
    def line(name, amount):
        return {"product_name": name, "order_quantity": 1, "product_special_price": amount,
                "ATC_total": amount, "server_total_price": 0, "ATC_WCL": 0, "product_category": "Service"}
    try:
        fee = float((row or {}).get("fee") or 0)
    except (TypeError, ValueError):
        fee = 0.0
    if fee:
        option = BY_KEY.get(row.get("option_key"), {})
        when = row.get("promised_date")
        when = when.strftime("%d %b %Y") if hasattr(when, "strftime") else ""
        out.append(line("Delivery: %s (18%% GST inclusive%s)" % (option.get("label", "paid option"),
                                                                "; promised by " + when if when else ""),
                        int(fee) if fee == int(fee) else fee))
    if dispatch_note:
        out.append(line("Dispatch instruction: %s" % dispatch_note, 0))
    return out
