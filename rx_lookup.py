#!/usr/bin/env python3
"""LOOKUP_PRESCRIPTION: the spectacle prescriptions a customer may be told.

Read-only, and scoped to what the requester already owns: the prescription
rows attached to lines of *this browser's* cart (the Flask session), and to
the recent orders of the customer *signed in on this browser* (never the id a
chat widget sent). The model gets the values as text in its prompt; it never
gets a query, an id it could change, or another customer's row.

A value is quoted exactly as stored in ``rx_collector`` (``SPH/CYL/AXIS/ADD``
per eye); nothing here rounds, infers or fills in a missing eye.
"""
ORDER_LOOKBACK_DAYS = 180
MAX_ORDERS = 4
MAX_CART_LINES = 6


def _num(v):
    s = str(v if v is not None else "").strip()
    if s in ("", "None", "null", "-", "NA", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_eye(value):
    """``"-1.25/-0.50/180/2.00"`` -> ``{sph, cyl, axis, add}`` (None where the
    part is absent). None when the string holds no power at all."""
    if not value:
        return None
    parts = (str(value).split("/") + [None, None, None, None])[:4]
    sph, cyl, axis, add = (_num(p) for p in parts)
    if sph is None and cyl is None and add is None:
        return None
    return {"sph": sph, "cyl": cyl, "axis": axis, "add": add}


def _signed(v):
    return "%+.2f" % v if v else "0.00"


def format_eye(eye):
    """One eye as a customer reads it; CYL/AXIS/ADD only when present."""
    if not eye:
        return "not entered"
    out = ["SPH %s" % _signed(eye["sph"] or 0.0)]
    if eye.get("cyl"):
        out.append("CYL %s" % _signed(eye["cyl"]))
        if eye.get("axis") is not None:
            out.append("AXIS %d" % int(eye["axis"]))
    if eye.get("add"):
        out.append("ADD %s" % _signed(eye["add"]))
    return " ".join(out)


def cart_rx_lines(cart):
    """``[(rx_id, product_id, product_name)]`` for cart lines carrying an rx."""
    out = []
    for item in cart or []:
        if not isinstance(item, dict):
            continue
        rx_id = item.get("rx_id")
        try:
            rx_id = int(rx_id)
        except (TypeError, ValueError):
            continue
        if rx_id <= 0:
            continue
        out.append((rx_id, str(item.get("product_id") or ""),
                    str(item.get("product_name") or "")[:80]))
    return out[:MAX_CART_LINES]


def _row(r, key, idx):
    return r[key] if isinstance(r, dict) else r[idx]


def read_model(cursor, cart, customer_id, site_from=None):
    """``{"cart": [...], "orders": [...]}``; each entry holds the product
    name, lens package and both eyes. The cart rows are matched on rx_id *and*
    product_id, so a crafted cart entry cannot read an unrelated row."""
    model = {"cart": [], "orders": []}
    lines = cart_rx_lines(cart)
    if lines:
        ids = [ln[0] for ln in lines]
        cursor.execute(
            "SELECT rx_id, product_id, right_eye, left_eye, recommendations "
            "FROM rx_collector WHERE rx_id IN (%s)" % ",".join(["%s"] * len(ids)),
            tuple(ids))
        by_id = {int(_row(r, "rx_id", 0)): r for r in cursor.fetchall()}
        for rx_id, product_id, name in lines:
            r = by_id.get(rx_id)
            if r is None or str(_row(r, "product_id", 1) or "") != product_id:
                continue
            model["cart"].append({
                "product_name": name,
                "lens": str(_row(r, "recommendations", 4) or "")[:60],
                "right": parse_eye(_row(r, "right_eye", 2)),
                "left": parse_eye(_row(r, "left_eye", 3))})
    if customer_id:
        sql = ("SELECT o.order_id, o.date_created, rc.right_eye, rc.left_eye, "
               "rc.recommendations, p.product_name, "
               "EXISTS(SELECT 1 FROM payment_collector pc WHERE pc.order_id=o.order_id "
               "AND pc.status='TXN_SUCCESS') AS paid "
               "FROM orders o JOIN rx_collector rc ON rc.rx_id=o.rx_id "
               "LEFT JOIN products p ON p.product_id=o.product_id "
               "WHERE o.customer_id=%s AND o.is_test=0 AND o.archived=0 "
               "AND o.date_created >= NOW() - INTERVAL " + str(int(ORDER_LOOKBACK_DAYS)) +
               " DAY ")
        params = [customer_id]
        if site_from:
            sql += "AND o.site_from=%s "
            params.append(site_from)
        sql += "ORDER BY o.date_created DESC LIMIT %d" % (MAX_ORDERS * 3)
        cursor.execute(sql, tuple(params))
        for r in cursor.fetchall():
            if len(model["orders"]) >= MAX_ORDERS:
                break
            right = parse_eye(_row(r, "right_eye", 2))
            left = parse_eye(_row(r, "left_eye", 3))
            if right is None and left is None:
                continue
            created = _row(r, "date_created", 1)
            model["orders"].append({
                "order_id": str(_row(r, "order_id", 0)),
                "date": created.strftime("%d %b %Y") if hasattr(created, "strftime")
                else str(created or "")[:10],
                "paid": bool(_row(r, "paid", 6)),
                "product_name": str(_row(r, "product_name", 5) or "")[:80],
                "lens": str(_row(r, "recommendations", 4) or "")[:60],
                "right": right, "left": left})
    return model


def found(model):
    return bool(model and (model.get("cart") or model.get("orders")))


def prompt_section(model, signed_in):
    """The block the model answers a prescription question from."""
    lines = ["", "PRESCRIPTIONS ON FILE (LOOKUP_PRESCRIPTION: read just now from this "
             "customer's own cart%s; authoritative):" % (" and orders" if signed_in else "")]
    for c in (model or {}).get("cart") or []:
        lines.append("  In the cart: %s%s — Right eye (OD): %s; Left eye (OS): %s"
                     % (c["product_name"] or "spectacle frame",
                        (", lens " + c["lens"]) if c["lens"] else "",
                        format_eye(c["right"]), format_eye(c["left"])))
    for o in (model or {}).get("orders") or []:
        lines.append("  Order %s (%s, %s): %s%s — Right eye (OD): %s; Left eye (OS): %s"
                     % (o["order_id"], o["date"],
                        "paid" if o["paid"] else "payment NOT completed, not in production",
                        o["product_name"] or "spectacle frame",
                        (", lens " + o["lens"]) if o["lens"] else "",
                        format_eye(o["right"]), format_eye(o["left"])))
    if not found(model):
        lines.append("  Nothing on file: no prescription is attached to this browser's cart"
                     + ("" if signed_in else " and the customer is not signed in, so orders "
                        "cannot be read") + ".")
    lines.append("  Glasses are made with exactly the prescription attached to that cart "
                 "line or order. Quote these values exactly; never change, round or add "
                 "one. An unpaid order is not being made yet. If a value is wrong: before "
                 "payment the customer edits it on the product page; after payment offer a "
                 "support ticket.")
    return "\n".join(lines) + "\n"


def event_payload(model, signed_in):
    """Counts only — no power, no order id."""
    m = model or {}
    return {"cart_lines": len(m.get("cart") or []),
            "order_lines": len(m.get("orders") or []),
            "unpaid_orders": sum(1 for o in m.get("orders") or [] if not o["paid"]),
            "signed_in": bool(signed_in), "found": found(m)}
