"""Optiwar order entry — lookups, order creation, and the equivalent SQL script.

Two ways to land an order in optiwar2, from the same draft:

* ``build_sql`` emits a script an operator runs by hand — the original path, and
  still the fallback whenever the write account is not configured.
* ``create_order`` performs the same writes itself, in one short transaction
  under the optiwar2 write account (see ``database.ow_write_transaction``).

Existing India-site addresses may have their state spelling canonicalized;
otherwise existing address rows are left unchanged. No path touches ``products`` —
inventory moves only when the customer pays, on the storefront. Drafts live in
the ops database (oslb6).
"""
import json
import logging
import time

import pymysql
import requests

from database import ow_cursor, ow_ops_cursor, ow_write_transaction, ow_write_enabled
from services import india_states

log = logging.getLogger("ow_new_order")

PRICING_API = "https://optiwar.com/api/lens-pricing"
SITES = {
    "optiwar.com": {"currency": "EUR", "symbol": "\u20ac", "price_field": "price_eur"},
    "in.optiwar.com": {"currency": "INR", "symbol": "\u20b9", "price_field": "price"},
}
LENS_CATEGORIES = ["bifocal", "coatings", "thickness"]

_catalogue_cache = {"at": 0.0, "data": None}


# ─── Read-only lookups on optiwar2 ───────────────────────────────────

IMAGE_BASE = "https://optiwar.com/static/"


def image_urls(product_image: str):
    """Absolute URLs for the comma-separated './catalog/...' paths in products."""
    out = []
    for part in (product_image or "").split(","):
        part = part.strip().lstrip(".").lstrip("/")
        if part:
            out.append(IMAGE_BASE + part)
    return out


def size_label(row: dict) -> str:
    """Frame size as eye-bridge-temple, from product_size or its components."""
    if row.get("product_size"):
        return str(row["product_size"]).strip()
    parts = [row.get("product_diameter"), row.get("product_bridge"),
             row.get("product_lenght")]
    return "-".join(str(p) for p in parts if p) if any(parts) else ""


PRODUCT_FIELDS = """
            product_id, product_code, product_name, product_category,
            product_price, product_special_price,
            product_price_eur, product_special_price_eur,
            product_quantity, product_status, discontinued, hsn,
            product_image, product_size, product_bridge, product_lenght,
            product_diameter, product_material, product_color, color_display,
            product_shape, product_style, product_gender, product_slug,
            fitting_range, form_factor, warranty"""


def search_products(q: str, limit: int = 25):
    """Search products by product_id, product_code or product_name (read-only)."""
    q = (q or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    pid = int(q) if q.isdigit() else -1
    with ow_cursor() as cur:
        cur.execute(f"""
            SELECT {PRODUCT_FIELDS}
            FROM products
            WHERE product_id = %s OR product_code LIKE %s OR product_name LIKE %s
            ORDER BY (product_id = %s) DESC, (product_code = %s) DESC, product_name
            LIMIT %s
        """, (pid, like, like, pid, q, limit))
        return cur.fetchall()


def get_product(product_id: int):
    with ow_cursor() as cur:
        cur.execute(f"""
            SELECT {PRODUCT_FIELDS}
            FROM products WHERE product_id = %s
        """, (product_id,))
        return cur.fetchone()


def search_customers(q: str, limit: int = 20):
    """Search customers by id, name, email or phone (read-only)."""
    q = (q or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    cid = int(q) if q.isdigit() else -1
    with ow_cursor() as cur:
        cur.execute("""
            SELECT c.customer_id, c.customer_name, c.customer_email, c.customer_phone,
                   c.date_created,
                   (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.customer_id) AS orders_count
            FROM customers c
            WHERE c.customer_id = %s OR c.customer_name LIKE %s
               OR c.customer_email LIKE %s OR c.customer_phone LIKE %s
            ORDER BY (c.customer_id = %s) DESC, c.customer_id DESC
            LIMIT %s
        """, (cid, like, like, like, cid, limit))
        return cur.fetchall()


def get_customer(customer_id: int):
    with ow_cursor() as cur:
        cur.execute("SELECT customer_id, customer_name, customer_email, customer_phone "
                    "FROM customers WHERE customer_id = %s", (customer_id,))
        return cur.fetchone()


def get_customer_addresses(customer_id: int):
    with ow_cursor() as cur:
        cur.execute("""
            SELECT address_id, address, address2, state, zipcode, country,
                   delivery_phone, delivery_email
            FROM customers_address WHERE customer_id = %s ORDER BY address_id DESC
        """, (customer_id,))
        return cur.fetchall()


def _get_customer_address(customer_id, address_id):
    try:
        customer_id = int(customer_id)
        address_id = int(address_id)
    except (TypeError, ValueError):
        return None
    for address in get_customer_addresses(customer_id):
        try:
            if int(address.get("address_id") or 0) == address_id:
                return address
        except (TypeError, ValueError):
            continue
    return None


def get_customer_rx(customer_id: int, limit: int = 30):
    """Prescriptions this customer has ordered before, newest first (read-only).

    rx_collector has no customer column, so ownership comes through orders."""
    with ow_cursor() as cur:
        cur.execute("""
            SELECT r.rx_id, r.right_eye, r.left_eye, r.product_id,
                   r.recommendations, r.recommendation_price,
                   r.addon_1_name, r.addon_1_price, r.addon_2_name, r.addon_2_price,
                   r.addon_3_name, r.addon_3_price, r.date_created,
                   p.product_code, p.product_name, MAX(o.order_id) AS order_id
            FROM orders o
            JOIN rx_collector r ON r.rx_id = o.rx_id
            LEFT JOIN products p ON p.product_id = r.product_id
            WHERE o.customer_id = %s
            GROUP BY r.rx_id
            ORDER BY r.rx_id DESC
            LIMIT %s
        """, (customer_id, limit))
        return cur.fetchall()


def next_order_id():
    """Candidate order id from optiwar2's own generator.

    generate_random_order_id() is declared READS SQL DATA — it only reads `orders`
    until it finds an unused id, so calling it writes nothing. The generated script
    re-checks the id before inserting in case one is taken meanwhile."""
    with ow_cursor() as cur:
        cur.execute("SELECT generate_random_order_id() AS oid")
        row = cur.fetchone() or {}
    return row.get("oid") or row.get("generate_random_order_id()")


def verify_order(order_id: str):
    """Read an order back from optiwar2 after the operator has run the script."""
    with ow_cursor() as cur:
        cur.execute("""
            SELECT o.order_line_id, o.order_id, o.date_created, o.order_quantity,
                   o.order_total, o.product_id, o.rx_id, o.customer_id, o.address_id,
                   o.site_from, o.is_test, o.fulfillment_status,
                   p.product_code, p.product_name
            FROM orders o LEFT JOIN products p ON p.product_id = o.product_id
            WHERE o.order_id = %s ORDER BY o.order_line_id
        """, (order_id,))
        lines = cur.fetchall()
        if not lines:
            return None
        cur.execute("""
            SELECT order_status_id, order_status_name, created_at, source, manual_flag, note
            FROM order_status WHERE order_id = %s ORDER BY order_status_id
        """, (order_id,))
        statuses = cur.fetchall()
        cur.execute("SELECT payment_ref, status, date_created FROM payment_collector "
                    "WHERE order_id = %s ORDER BY id", (order_id,))
        payments = cur.fetchall()
        cur.execute("SELECT c.customer_id, c.customer_name, c.customer_email, c.customer_phone "
                    "FROM customers c WHERE c.customer_id = %s", (lines[0]["customer_id"],))
        customer = cur.fetchone() or {}
        addr = {}
        if lines[0].get("address_id"):
            cur.execute("SELECT address_id, address, address2, state, zipcode, country, "
                        "delivery_phone, delivery_email FROM customers_address "
                        "WHERE address_id = %s", (lines[0]["address_id"],))
            addr = cur.fetchone() or {}
    return {"lines": lines, "statuses": statuses, "payments": payments,
            "customer": customer, "address": addr,
            "total": sum(int(l["order_total"] or 0) for l in lines)}


# ─── Lens catalogue (live prices, same API as the Lens Pricing page) ──

def lens_catalogue(force: bool = False):
    """Fetch the live lens/add-on catalogue. Cached for 5 minutes."""
    now = time.time()
    if not force and _catalogue_cache["data"] and now - _catalogue_cache["at"] < 300:
        return _catalogue_cache["data"]
    try:
        resp = requests.get(PRICING_API, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        _catalogue_cache.update({"at": now, "data": data})
        return data
    except Exception as e:
        log.warning("lens catalogue fetch failed: %s", e)
        return _catalogue_cache["data"] or {}


def lens_options(site: str, bifocal_context: bool = False):
    """Flatten the catalogue into pickable options priced for the chosen site."""
    cat = lens_catalogue()
    ctx_key = "when_bifocal" if bifocal_context else "default"
    ctx = cat.get(ctx_key) or cat.get("default") or {}
    field = SITES.get(site, SITES["in.optiwar.com"])["price_field"]
    out = {}
    for category in LENS_CATEGORIES:
        items = []
        for code, item in (ctx.get(category) or {}).items():
            price = item.get(field)
            if price is None:
                price = item.get("price", 0)
            items.append({"code": code, "name": item.get("name", code),
                          "desc": item.get("desc", ""), "price": float(price or 0)})
        items.sort(key=lambda i: i["name"])
        if items:
            out[category] = items
    return out


# ─── Draft storage (ops database) ────────────────────────────────────

def new_draft(actor_id=None, site: str = "in.optiwar.com"):
    payload = {"site": site, "customer": {"mode": "new"}, "address": {"mode": "new"},
               "lines": [], "note": "", "is_test": 0, "pay_link": ""}
    with ow_ops_cursor() as cur:
        cur.execute("INSERT INTO ops_order_drafts (payload, created_by) VALUES (%s, %s)",
                    (json.dumps(payload), actor_id))
        return cur.lastrowid


def get_draft(draft_id: int):
    with ow_cursor() as cur:
        cur.execute("SELECT * FROM ops_order_drafts WHERE id = %s", (draft_id,))
        row = cur.fetchone()
    if not row:
        return None
    row["data"] = json.loads(row["payload"])
    return row


def list_drafts(limit: int = 30):
    with ow_cursor() as cur:
        cur.execute("""
            SELECT id, order_id, state, created_at, generated_at, verified_at, notified_at, payload
            FROM ops_order_drafts ORDER BY id DESC LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
    for r in rows:
        try:
            d = json.loads(r["payload"])
        except ValueError:
            d = {}
        r["site"] = d.get("site", "")
        r["lines_count"] = len(d.get("lines") or [])
        r["customer_name"] = (d.get("customer") or {}).get("name", "")
        r["total"] = draft_total(d)
    return rows


def save_draft(draft_id: int, data: dict, state: str = None, order_id: str = None,
               sql_text: str = None, stamp: str = None):
    sets = ["payload = %s"]
    vals = [json.dumps(data)]
    if state:
        sets.append("state = %s")
        vals.append(state)
    if order_id is not None:
        sets.append("order_id = %s")
        vals.append(order_id)
    if sql_text is not None:
        sets.append("sql_text = %s")
        vals.append(sql_text)
    if stamp in ("generated_at", "verified_at", "notified_at"):
        sets.append(f"{stamp} = NOW()")
    vals.append(draft_id)
    with ow_ops_cursor() as cur:
        cur.execute(f"UPDATE ops_order_drafts SET {', '.join(sets)} WHERE id = %s", vals)


def delete_draft(draft_id: int):
    with ow_ops_cursor() as cur:
        cur.execute("DELETE FROM ops_order_drafts WHERE id = %s", (draft_id,))


# ─── Prescription formatting ─────────────────────────────────────────

def normalize_rx_eye(raw) -> str:
    """An eye power as the storefront stores it: ``SPH/CYL/AXIS/ADD``.

    Checkout always writes four slash-separated parts, and every reader relies
    on that: the confirmation page hides the prescription entirely when the
    value has no slash, so an operator typing just ``-2.00`` produced an order
    whose RX the customer could not see. Missing parts become ``0`` — no
    cylinder, no axis, no addition — which is what a sphere-only power means."""
    text = str(raw or "").strip()
    if not text:
        return ""
    parts = [p.strip() or "0" for p in text.split("/")]
    while len(parts) < 4:
        parts.append("0")
    return "/".join(parts[:4])


# ─── Pricing / totals ────────────────────────────────────────────────

def line_total(line: dict) -> int:
    """Frame + lens + add-ons, times quantity. orders.order_total is an INT."""
    if line.get("total_override") not in (None, ""):
        return int(round(float(line["total_override"])))
    unit = float(line.get("unit_price") or 0)
    lens = float(line.get("recommendation_price") or 0)
    addons = sum(float(a.get("price") or 0) for a in (line.get("addons") or []))
    qty = int(line.get("qty") or 1)
    return int(round((unit + lens + addons) * qty))


def draft_total(data: dict) -> int:
    return sum(line_total(l) for l in (data.get("lines") or []))


def product_price_options(product: dict, site: str):
    """The two catalogue prices for the chosen site, for the price-basis selector."""
    if SITES.get(site, {}).get("currency") == "EUR":
        return {"list": float(product.get("product_price_eur") or 0),
                "special": float(product.get("product_special_price_eur") or 0)}
    return {"list": float(product.get("product_price") or 0),
            "special": float(product.get("product_special_price") or 0)}


# ─── SQL generation ──────────────────────────────────────────────────

def _q(v) -> str:
    """Quote a value as a SQL literal. NULL for empty, escaped single-quoted string."""
    if v is None or v == "":
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace("'", "''")
    return f"'{s}'"


def _ascii(v) -> str:
    """order_history is latin1 — keep audit text ASCII."""
    kept = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in str(v or ""))
    return " ".join(kept.split())


def validate_draft(data: dict):
    """Return a list of blocking problems with the draft."""
    errs = []
    site = data.get("site")
    if site not in SITES:
        errs.append("Choose the site the order belongs to (optiwar.com or in.optiwar.com).")
    cust = data.get("customer") or {}
    if cust.get("mode") == "existing":
        if not cust.get("customer_id"):
            errs.append("Select an existing customer or switch to a new one.")
    else:
        if not (cust.get("name") or "").strip():
            errs.append("Customer name is required.")
        if not (cust.get("email") or "").strip():
            errs.append("Customer email is required (customers.customer_email is NOT NULL).")
        if not (cust.get("phone") or "").strip():
            errs.append("Customer phone is required to send the order notification.")
    addr = data.get("address") or {}
    if addr.get("mode") == "existing":
        if not addr.get("address_id"):
            errs.append("Select a delivery address or enter a new one.")
        elif site == "in.optiwar.com":
            saved_address = _get_customer_address(
                cust.get("customer_id"), addr.get("address_id"))
            if saved_address is not None:
                saved_state = saved_address.get("state")
                if india_states.normalize(saved_state) is None:
                    errs.append(
                        f"Saved address state '{saved_state}' is not one of the "
                        "36 Indian states: add a new address with the correct state.")
    else:
        for field, label in (("address", "Address line 1"),
                             ("zipcode", "Zipcode"), ("country", "Country")):
            if not (addr.get(field) or "").strip():
                errs.append(f"{label} is required.")
        if site == "in.optiwar.com":
            try:
                addr["state"] = india_states.require(addr.get("state"))
            except india_states.InvalidState:
                errs.append("State: choose one of the 36 Indian states")
        elif not (addr.get("state") or "").strip():
            errs.append("State is required.")
        if site == "in.optiwar.com" and (addr.get("country") or "").strip().lower() not in (
                "india", "in", "bharat"):
            errs.append("in.optiwar.com orders must ship to an Indian address.")
    if not (data.get("lines") or []):
        errs.append("Add at least one product line.")
    for i, line in enumerate(data.get("lines") or [], start=1):
        if not line.get("product_id"):
            errs.append(f"Line {i}: product missing.")
        if line_total(line) <= 0:
            errs.append(f"Line {i}: total is zero — set a price basis or an override.")
        if line.get("rx_mode") == "existing" and not line.get("rx_id"):
            errs.append(f"Line {i}: choose the existing prescription or enter a new one.")
    return errs


def build_sql(data: dict, order_id: str, actor: str = "ops") -> str:
    """Build the transactional script that creates this order in optiwar2.

    Status is written as Pending with no payment row — payment lands in
    payment_collector when the customer pays the link."""
    site = data.get("site", "in.optiwar.com")
    cust = data.get("customer") or {}
    addr = data.get("address") or {}
    address_state = addr.get("state")
    existing_state_update = None
    if addr.get("mode") != "existing" and site == "in.optiwar.com":
        address_state = india_states.require(address_state)
    elif addr.get("mode") == "existing" and site == "in.optiwar.com":
        saved_address = _get_customer_address(
            cust.get("customer_id"), addr.get("address_id"))
        if saved_address is not None:
            saved_state = saved_address.get("state")
            normalized_state = india_states.normalize(saved_state)
            if normalized_state is not None and normalized_state != saved_state:
                existing_state_update = normalized_state
    lines = data.get("lines") or []
    note = _ascii(data.get("note") or "")
    total = draft_total(data)
    cur_sym = SITES[site]["currency"]

    out = []
    a = out.append
    a("-- ─────────────────────────────────────────────────────────────")
    a(f"-- Optiwar order {order_id} — generated by the ops platform")
    a(f"-- site: {site}   lines: {len(lines)}   total: {cur_sym} {total}")
    a(f"-- prepared by: {actor}")
    a("-- Run on the optiwar2 database. Review before executing.")
    a("-- Creates the order as Pending with NO payment row: payment_collector is")
    a("-- written by the gateway when the customer pays.")
    a("-- ─────────────────────────────────────────────────────────────")
    a("")
    a("-- Step 0 — the order id must still be free (it was free when generated).")
    a(f"SELECT COUNT(*) AS must_be_zero FROM orders WHERE order_id = {_q(order_id)};")
    a("")
    a("START TRANSACTION;")
    a("")
    a(f"SET @order_id = {_q(order_id)};")

    if cust.get("mode") == "existing":
        a(f"SET @customer_id = {int(cust['customer_id'])};   -- existing customer "
          f"{_ascii(cust.get('name') or '')}")
        a("-- confirm the customer still exists")
        a("SELECT customer_id, customer_name, customer_email, customer_phone "
          "FROM customers WHERE customer_id = @customer_id;")
    else:
        a("-- new customer")
        a("INSERT INTO customers (customer_name, customer_email, customer_phone)")
        a(f"VALUES ({_q(cust.get('name'))}, {_q(cust.get('email'))}, {_q(cust.get('phone'))});")
        a("SET @customer_id = LAST_INSERT_ID();")
    a("")

    if addr.get("mode") == "existing":
        a(f"SET @address_id = {int(addr['address_id'])};   -- existing address")
        a("SELECT address_id, address, state, zipcode, country FROM customers_address "
          "WHERE address_id = @address_id AND customer_id = @customer_id;")
        if existing_state_update is not None:
            a(f"UPDATE customers_address SET state = {_q(existing_state_update)} "
              "WHERE address_id = @address_id AND customer_id = @customer_id;")
    else:
        a("-- delivery address")
        a("INSERT INTO customers_address (customer_id, address, address2, state, zipcode,")
        a("                              country, delivery_phone, delivery_email)")
        a(f"VALUES (@customer_id, {_q(addr.get('address'))}, {_q(addr.get('address2'))}, "
          f"{_q(address_state)}, {_q(addr.get('zipcode'))},")
        a(f"        {_q(addr.get('country'))}, {_q(addr.get('delivery_phone'))}, "
          f"{_q(addr.get('delivery_email'))});")
        a("SET @address_id = LAST_INSERT_ID();")
    a("")

    for idx, line in enumerate(lines, start=1):
        pid = int(line["product_id"])
        qty = int(line.get("qty") or 1)
        ltot = line_total(line)
        label = _ascii(f"{line.get('product_code','')} {line.get('product_name','')}").strip()
        a(f"-- line {idx}: {label} x{qty} = {cur_sym} {ltot}")
        if line.get("rx_mode") == "existing":
            a(f"SET @rx_{idx} = {int(line['rx_id'])};   -- reused prescription, left unchanged")
            a(f"SELECT rx_id, right_eye, left_eye, recommendations FROM rx_collector "
              f"WHERE rx_id = @rx_{idx};")
        else:
            addons = (line.get("addons") or []) + [{}, {}, {}]
            a("INSERT INTO rx_collector (right_eye, left_eye, product_id, recommendations,")
            a("                          recommendation_price, addon_1_name, addon_1_price,")
            a("                          addon_2_name, addon_2_price, addon_3_name, addon_3_price)")
            a(f"VALUES ({_q(normalize_rx_eye(line.get('right_eye')))}, "
              f"{_q(normalize_rx_eye(line.get('left_eye')))}, {pid}, "
              f"{_q(line.get('recommendations'))},")
            a(f"        {int(round(float(line.get('recommendation_price') or 0)))}, "
              f"{_q(addons[0].get('name'))}, {int(round(float(addons[0].get('price') or 0)))},")
            a(f"        {_q(addons[1].get('name'))}, {int(round(float(addons[1].get('price') or 0)))}, "
              f"{_q(addons[2].get('name'))}, {int(round(float(addons[2].get('price') or 0)))});")
            a(f"SET @rx_{idx} = LAST_INSERT_ID();")
        a("INSERT INTO orders (order_id, order_quantity, order_total, rx_id, product_id,")
        a("                    customer_id, address_id, ip_address, site_from, is_test,")
        a("                    fulfillment_status)")
        a(f"VALUES (@order_id, {qty}, {ltot}, @rx_{idx}, {pid},")
        a(f"        @customer_id, @address_id, NULL, {_q(site)}, "
          f"{1 if data.get('is_test') else 0}, 'pending');")
        a("")

    status_note = _ascii("created by ops - awaiting payment" + (f" - {note}" if note else ""))
    a("-- initial status: append-only, never supply order_status_id")
    a("INSERT INTO order_status (order_status_name, order_id, source, manual_flag, note)")
    a(f"VALUES ('Pending', @order_id, 'ops', 1, {_q(status_note[:255])});")
    a("")
    a("-- audit line (order_history is latin1: ASCII only)")
    hist = _ascii(f"Order created by ops for customer - payment link sent"
                  + (f" - {note}" if note else ""))
    a("INSERT INTO order_history (order_history_content, order_id)")
    a(f"VALUES ({_q(hist[:255])}, @order_id);")
    a("")
    a("COMMIT;")
    a("")
    a("-- Verification")
    a("SELECT o.order_id, o.order_line_id, o.product_id, o.order_quantity, o.order_total,")
    a("       o.rx_id, o.customer_id, o.address_id, o.site_from, o.is_test,")
    a("       o.fulfillment_status")
    a("FROM orders o WHERE o.order_id = @order_id ORDER BY o.order_line_id;")
    a("")
    a("SELECT order_status_id, order_status_name, created_at, source, manual_flag, note")
    a("FROM order_status WHERE order_id = @order_id ORDER BY order_status_id;")
    a("")
    a(f"SELECT SUM(order_total) AS grand_total  -- expected: {total}")
    a("FROM orders WHERE order_id = @order_id;")
    a("")
    a("-- No payment_collector row is created here. It appears when the customer pays.")
    return "\n".join(out) + "\n"


# ─── Order creation (INSERT-only account, one short transaction) ──────

DEADLOCK_ERRNOS = (1205, 1213)   # lock wait timeout, deadlock
CREATE_ATTEMPTS = 3


def claim_draft_for_create(draft_id: int):
    """Take the draft for creation, so a double-click cannot make two orders.

    Returns 'claimed' to proceed, 'exists' if the order was already created, or
    'busy' if another request is mid-creation. The UPDATE is the lock: only one
    caller can move a draft out of its pre-creation state."""
    with ow_ops_cursor() as cur:
        cur.execute("""
            UPDATE ops_order_drafts SET state = 'creating'
            WHERE id = %s AND state IN ('draft', 'generated', 'create_failed')
        """, (draft_id,))
        if cur.rowcount == 1:
            return "claimed"
        cur.execute("SELECT state FROM ops_order_drafts WHERE id = %s", (draft_id,))
        row = cur.fetchone() or {}
    return "busy" if row.get("state") == "creating" else "exists"


def release_draft_claim(draft_id: int, state: str = "create_failed"):
    with ow_ops_cursor() as cur:
        cur.execute("UPDATE ops_order_drafts SET state = %s WHERE id = %s AND state = 'creating'",
                    (state, draft_id))


def _order_id_is_free(cur, order_id: str) -> bool:
    cur.execute("SELECT 1 FROM orders WHERE order_id = %s LIMIT 1", (order_id,))
    return cur.fetchone() is None


def _insert_order(cur, data: dict, order_id: str, actor: str):
    """The writes, in the order build_sql emits them. Runs inside a transaction.

    Reads inside here are plain SELECTs under READ COMMITTED: no FOR UPDATE, so a
    live checkout never waits on this."""
    site = data.get("site", "in.optiwar.com")
    cust = data.get("customer") or {}
    addr = data.get("address") or {}
    address_state = addr.get("state")
    if addr.get("mode") != "existing" and site == "in.optiwar.com":
        address_state = india_states.require(address_state)
    note = _ascii(data.get("note") or "")

    if cust.get("mode") == "existing":
        customer_id = int(cust["customer_id"])
        cur.execute("SELECT customer_id FROM customers WHERE customer_id = %s", (customer_id,))
        if not cur.fetchone():
            raise ValueError(f"customer {customer_id} no longer exists in optiwar2")
    else:
        cur.execute("INSERT INTO customers (customer_name, customer_email, customer_phone) "
                    "VALUES (%s, %s, %s)",
                    (cust.get("name"), cust.get("email"), cust.get("phone")))
        customer_id = cur.lastrowid

    if addr.get("mode") == "existing":
        address_id = int(addr["address_id"])
        address_fields = "address_id, state" if site == "in.optiwar.com" else "address_id"
        cur.execute(f"SELECT {address_fields} FROM customers_address "
                    "WHERE address_id = %s AND customer_id = %s", (address_id, customer_id))
        saved_address = cur.fetchone()
        if not saved_address:
            raise ValueError(f"address {address_id} does not belong to customer {customer_id}")
        if site == "in.optiwar.com":
            saved_state = saved_address.get("state")
            normalized_state = india_states.normalize(saved_state)
            if normalized_state is not None and normalized_state != saved_state:
                cur.execute(
                    "UPDATE customers_address SET state=%s WHERE address_id=%s AND customer_id=%s",
                    (normalized_state, address_id, customer_id))
    else:
        cur.execute("""
            INSERT INTO customers_address (customer_id, address, address2, state, zipcode,
                                           country, delivery_phone, delivery_email)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (customer_id, addr.get("address"), addr.get("address2") or None,
              address_state, addr.get("zipcode"), addr.get("country"),
              addr.get("delivery_phone") or None, addr.get("delivery_email") or None))
        address_id = cur.lastrowid

    for line in (data.get("lines") or []):
        product_id = int(line["product_id"])
        qty = int(line.get("qty") or 1)
        ltot = line_total(line)
        if line.get("rx_mode") == "existing":
            rx_id = int(line["rx_id"])
            cur.execute("SELECT rx_id FROM rx_collector WHERE rx_id = %s", (rx_id,))
            if not cur.fetchone():
                raise ValueError(f"prescription {rx_id} no longer exists in optiwar2")
        else:
            addons = (line.get("addons") or []) + [{}, {}, {}]
            cur.execute("""
                INSERT INTO rx_collector (right_eye, left_eye, product_id, recommendations,
                                          recommendation_price, addon_1_name, addon_1_price,
                                          addon_2_name, addon_2_price, addon_3_name,
                                          addon_3_price)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (normalize_rx_eye(line.get("right_eye")),
                  normalize_rx_eye(line.get("left_eye")), product_id,
                  line.get("recommendations"),
                  int(round(float(line.get("recommendation_price") or 0))),
                  addons[0].get("name"), int(round(float(addons[0].get("price") or 0))),
                  addons[1].get("name"), int(round(float(addons[1].get("price") or 0))),
                  addons[2].get("name"), int(round(float(addons[2].get("price") or 0)))))
            rx_id = cur.lastrowid
        cur.execute("""
            INSERT INTO orders (order_id, order_quantity, order_total, rx_id, product_id,
                                customer_id, address_id, ip_address, site_from, is_test,
                                fulfillment_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, 'pending')
        """, (order_id, qty, ltot, rx_id, product_id, customer_id, address_id,
              site, 1 if data.get("is_test") else 0))

    status_note = _ascii("created by ops - awaiting payment" + (f" - {note}" if note else ""))
    cur.execute("INSERT INTO order_status (order_status_name, order_id, source, manual_flag, note) "
                "VALUES ('Pending', %s, 'ops', 1, %s)", (order_id, status_note[:255]))
    hist = _ascii(f"Order created by {actor} from the ops platform - awaiting payment"
                  + (f" - {note}" if note else ""))
    cur.execute("INSERT INTO order_history (order_history_content, order_id) VALUES (%s, %s)",
                (hist[:255], order_id))
    return {"customer_id": customer_id, "address_id": address_id}


def create_order(data: dict, order_id: str = "", actor: str = "ops"):
    """Create the order in optiwar2 and read it back. Returns (ok, result).

    On success ``result`` is the verify_order() payload plus the order id. On
    failure nothing was committed: the transaction is atomic, so a partial order
    cannot survive."""
    if not ow_write_enabled():
        return False, "The optiwar2 write account is not configured (OW_WRITE_DB_USER)."
    errs = validate_draft(data)
    if errs:
        return False, "; ".join(errs)

    last = ""
    for attempt in range(1, CREATE_ATTEMPTS + 1):
        oid = order_id or next_order_id()
        try:
            with ow_write_transaction() as cur:
                if not _order_id_is_free(cur, oid):
                    # Taken since it was generated. A fresh id, not a failure.
                    order_id = ""
                    last = f"order id {oid} was taken"
                    continue
                _insert_order(cur, data, oid, actor)
        except pymysql.err.OperationalError as exc:
            code = exc.args[0] if exc.args else 0
            if code in DEADLOCK_ERRNOS:
                # Whole transaction rolled back, so retrying is safe. The wait is
                # almost always the orders->products foreign key check queueing
                # behind a checkout deducting that product's stock.
                log.warning("optiwar2 order create contended (%s), attempt %s", code, attempt)
                if attempt < CREATE_ATTEMPTS:
                    time.sleep(0.4 * attempt)
                    last = str(exc)
                    continue
                return False, (
                    f"optiwar2 was too busy to take the order after {CREATE_ATTEMPTS} "
                    "attempts — nothing was written, try again in a moment"
                )
            log.error("optiwar2 order create failed: %s", exc)
            return False, f"optiwar2 refused the write: {exc}"
        except (pymysql.MySQLError, ValueError) as exc:
            log.error("optiwar2 order create failed: %s", exc)
            return False, str(exc)

        found = verify_order(oid)
        if not found:
            return False, (f"order {oid} committed but could not be read back — "
                           "check optiwar2 before retrying")
        found["order_id"] = oid
        expected = draft_total(data)
        if found["total"] != expected:
            return False, (f"order {oid} was created but totals disagree "
                           f"(read {found['total']}, expected {expected})")
        return True, found

    return False, f"could not create the order after {CREATE_ATTEMPTS} attempts ({last})"
