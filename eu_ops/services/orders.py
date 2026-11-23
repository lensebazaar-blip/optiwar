"""Order management service — reads from OpenCart oslb6 database."""
import json
from database import db_cursor, db_transaction, ow_cursor
import config


def list_orders(page: int = 1, per_page: int = 25, status_id: int = None,
                search: str = None, store_id: int = 0):
    offset = (page - 1) * per_page
    where = ["o.order_status_id > 0"]
    params = []

    if store_id is not None:
        where.append("o.store_id = %s")
        params.append(store_id)

    if status_id:
        where.append("o.order_status_id = %s")
        params.append(status_id)

    if search:
        # Strip E- prefix if user searches for EU order ID format (e.g. E-151907)
        search_clean = search.strip()
        if search_clean.upper().startswith("E-"):
            search_clean = search_clean[2:]
        where.append(
            "(o.order_id = %s OR o.firstname LIKE %s OR o.lastname LIKE %s OR o.email LIKE %s)"
        )
        params.extend([search_clean, f"%{search_clean}%", f"%{search_clean}%", f"%{search_clean}%"])

    where_sql = " AND ".join(where)

    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) as cnt FROM `order` o WHERE {where_sql}", params)
        total = cur.fetchone()["cnt"]

        cur.execute(f"""
            SELECT o.order_id, o.store_id, o.store_name,
                   o.firstname, o.lastname, o.email, o.telephone,
                   o.total, o.currency_value, o.order_status_id, o.date_added, o.date_modified,
                   o.payment_method, o.shipping_method,
                   o.shipping_firstname, o.shipping_lastname,
                   o.shipping_city, o.shipping_country, o.shipping_postcode,
                   os.name as status_name
            FROM `order` o
            LEFT JOIN order_status os ON o.order_status_id = os.order_status_id AND os.language_id = 1
            WHERE {where_sql}
            ORDER BY o.order_id DESC
            LIMIT %s OFFSET %s
        """, params + [per_page, offset])
        orders = cur.fetchall()

    # Convert totals to EUR using order's own currency_value (rate at purchase time)
    for o in orders:
        eur_rate = float(o.get("currency_value") or 0.0108)
        o["total_eur"] = round(float(o["total"]) * eur_rate, 2)
        o["total_inr"] = float(o["total"])

    return {
        "orders": orders,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page,
    }


def get_order(order_id: int):
    with db_cursor() as cur:
        cur.execute("""
            SELECT o.*,
                   os.name as status_name,
                   c.iso_code_2 as shipping_country_code,
                   z.code as shipping_zone_code,
                   o.currency_value
            FROM `order` o
            LEFT JOIN order_status os ON o.order_status_id = os.order_status_id AND os.language_id = 1
            LEFT JOIN country c ON o.shipping_country_id = c.country_id
            LEFT JOIN zone z ON o.shipping_zone_id = z.zone_id
            WHERE o.order_id = %s
        """, (order_id,))
        order = cur.fetchone()

        if not order:
            return None

        eur_rate = float(order.get("currency_value") or 0.0108)
        order["total_eur"] = round(float(order["total"]) * eur_rate, 2)
        order["total_inr"] = float(order["total"])

        # Get products
        cur.execute("""
            SELECT op.*, p.image
            FROM order_product op
            LEFT JOIN product p ON op.product_id = p.product_id
            WHERE op.order_id = %s
        """, (order_id,))
        order["products"] = cur.fetchall()

        # Get product options (powers, quantities, etc.)
        for prod in order["products"]:
            cur.execute("""
                SELECT order_option_id, name, value, type FROM order_option
                WHERE order_product_id = %s
                ORDER BY order_option_id
            """, (prod["order_product_id"],))
            raw_options = cur.fetchall()
            prod["options"] = raw_options

            # Group options by eye (left/right) based on sequential position.
            # Options between LEFT POWER and RIGHT POWER belong to left eye;
            # options after RIGHT POWER belong to right eye.
            left_eye = []
            right_eye = []
            current_eye = None
            for opt in raw_options:
                name_upper = opt["name"].strip().upper()
                if "LEFT" in name_upper:
                    current_eye = "left"
                elif "RIGHT" in name_upper:
                    current_eye = "right"
                if current_eye == "left":
                    left_eye.append(opt)
                elif current_eye == "right":
                    right_eye.append(opt)
            prod["left_eye"] = left_eye
            prod["right_eye"] = right_eye
            prod["has_eye_options"] = bool(left_eye or right_eye)

        # Get totals
        cur.execute("SELECT * FROM order_total WHERE order_id = %s ORDER BY sort_order", (order_id,))
        order["totals"] = cur.fetchall()

        # Get history
        cur.execute("""
            SELECT oh.*, os.name as status_name
            FROM order_history oh
            LEFT JOIN order_status os ON oh.order_status_id = os.order_status_id AND os.language_id = 1
            WHERE oh.order_id = %s
            ORDER BY oh.date_added DESC
        """, (order_id,))
        order["history"] = cur.fetchall()

        # Get AWBs
        cur.execute("""
            SELECT sa.*, pu.username as created_by_name
            FROM shipping_awb sa
            LEFT JOIN platform_users pu ON sa.created_by = pu.id
            WHERE sa.order_id = %s
            ORDER BY sa.created_at DESC
        """, (order_id,))
        order["awbs"] = cur.fetchall()

        # Get pending approvals
        cur.execute("""
            SELECT ar.*, pu.username as requested_by_name
            FROM approval_requests ar
            LEFT JOIN platform_users pu ON ar.requested_by = pu.id
            WHERE ar.order_id = %s
            ORDER BY ar.created_at DESC
        """, (order_id,))
        order["approvals"] = cur.fetchall()

        # Get supplier assignment
        cur.execute("""
            SELECT * FROM supplier_order
            WHERE order_id = %s
            ORDER BY date_modified DESC LIMIT 1
        """, (str(order_id),))
        order["supplier"] = cur.fetchone()

    return order


def update_order_status(order_id: int, status_id: int, comment: str = "",
                        notify: int = 0, tracking: str = "", awb: str = "",
                        courier: str = "", added_by: str = "",
                        tracking_number: str = None):
    if tracking_number is not None:
        tracking = tracking_number
    with db_cursor() as cur:
        cur.execute(
            "UPDATE `order` SET order_status_id = %s WHERE order_id = %s",
            (status_id, order_id),
        )
        cur.execute("""
            INSERT INTO order_history
                (order_id, order_status_id, notify, comment, date_added,
                 tracking_number, awb, courier, weight, addedby)
            VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, '', %s)
        """, (order_id, status_id, notify, comment, tracking, awb, courier, added_by))
        return cur.lastrowid


def get_order_statuses():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM order_status WHERE language_id = 1 ORDER BY order_status_id")
        return cur.fetchall()


def get_supplier_list():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM supplier_list ORDER BY name")
        return cur.fetchall()


def assign_supplier(order_id: int, supplier_name: str, product_list: str = "",
                    reference: str = "", expected_ship_date: str = ""):
    exp = expected_ship_date or None
    with db_cursor() as cur:
        # Auto-generate plate reference if not provided
        if not reference:
            cur.execute(
                "SELECT COUNT(*) as cnt FROM supplier_order WHERE supplier_name = %s",
                (supplier_name,))
            count = cur.fetchone()["cnt"]
            reference = f"Plate #{count + 1}"

        # Check if assignment already exists
        cur.execute("SELECT supplier_id FROM supplier_order WHERE order_id = %s",
                    (str(order_id),))
        existing = cur.fetchone()
        if existing:
            cur.execute("""
                UPDATE supplier_order
                SET supplier_name = %s, supplier_product_list = %s,
                    supplier_reference = %s, expected_ship_date = %s, date_modified = NOW()
                WHERE order_id = %s
            """, (supplier_name, product_list, reference, exp, str(order_id)))
        else:
            cur.execute("""
                INSERT INTO supplier_order
                    (order_id, supplier_name, supplier_date, supplier_reference,
                     supplier_product_list, expected_ship_date, date_modified)
                VALUES (%s, %s, NOW(), %s, %s, %s, NOW())
            """, (str(order_id), supplier_name, reference, product_list, exp))
        return reference


def add_supplier(name: str, email: str = "", address: str = "", phone: str = ""):
    """Insert a new supplier into supplier_list. Returns new id or None if name blank/dupe."""
    name = (name or "").strip()
    if not name:
        return None
    with db_cursor() as cur:
        cur.execute("SELECT id FROM supplier_list WHERE name = %s", (name,))
        if cur.fetchone():
            return None
        phone_val = 0
        if phone and str(phone).strip().isdigit():
            phone_val = int(str(phone).strip())
        cur.execute("""
            INSERT INTO supplier_list (name, email, address, phone, pin)
            VALUES (%s, %s, %s, %s, %s)
        """, (name, email.strip(), address.strip(), phone_val, 0))
        return cur.lastrowid


def list_notes(page: int = 1, per_page: int = 30, supplier: str = None,
               status: str = "active", search: str = None):
    offset = (page - 1) * per_page
    base_where = []
    base_params = []

    if supplier:
        base_where.append("supplier_name = %s")
        base_params.append(supplier)

    if search:
        search_clean = search.strip()
        if search_clean.upper().startswith("NEP-"):
            search_clean = search_clean[4:]
        base_where.append("order_id LIKE %s")
        base_params.append(f"%{search_clean}%")

    base_sql = " AND ".join(base_where) if base_where else "1=1"

    # Add status filter for main query
    filtered_where = list(base_where)
    filtered_params = list(base_params)
    if status == "active":
        filtered_where.append("is_done = 0")
    elif status == "done":
        filtered_where.append("is_done = 1")
    filtered_sql = " AND ".join(filtered_where) if filtered_where else "1=1"

    with db_cursor() as cur:
        # Counts use base filter (no status) so both active/done are accurate
        cur.execute(
            f"SELECT COUNT(*) as cnt FROM supplier_order WHERE {base_sql} AND is_done = 0",
            base_params)
        active_count = cur.fetchone()["cnt"]

        cur.execute(
            f"SELECT COUNT(*) as cnt FROM supplier_order WHERE {base_sql} AND is_done = 1",
            base_params)
        done_count = cur.fetchone()["cnt"]

        # Total for filtered view (with status)
        cur.execute(f"SELECT COUNT(*) as cnt FROM supplier_order WHERE {filtered_sql}",
                    filtered_params)
        total = cur.fetchone()["cnt"]

        cur.execute(f"""
            SELECT * FROM supplier_order
            WHERE {filtered_sql}
            ORDER BY date_modified DESC
            LIMIT %s OFFSET %s
        """, filtered_params + [per_page, offset])
        notes = cur.fetchall()

    return {
        "notes": notes,
        "total": total,
        "active_count": active_count,
        "done_count": done_count,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page,
    }


def toggle_note_done(supplier_id: int, is_done: int):
    with db_cursor() as cur:
        cur.execute(
            "UPDATE supplier_order SET is_done = %s, date_modified = NOW() WHERE supplier_id = %s",
            (is_done, supplier_id))
        return cur.rowcount > 0


def get_order_counts_by_status(store_id: int = 0):
    with db_cursor() as cur:
        cur.execute("""
            SELECT o.order_status_id, os.name as status_name, COUNT(*) as cnt
            FROM `order` o
            LEFT JOIN order_status os ON o.order_status_id = os.order_status_id AND os.language_id = 1
            WHERE o.order_status_id > 0 AND o.store_id = %s
            GROUP BY o.order_status_id
            ORDER BY cnt DESC
        """, (store_id,))
        return cur.fetchall()


def _paid_reship_rows(ow_rows, search, lifecycle):
    from services import reship as reship_svc
    by_id = {str(r["order_id"]).upper(): r for r in ow_rows}
    out = []
    for oid, info in lifecycle.items():
        if not info["paid"]:
            continue
        e = info["entry"]
        base = by_id.get(oid)
        if base is None:
            with ow_cursor() as cur:
                cur.execute("""SELECT o.order_id, MAX(c.customer_name) AS customer,
                                      MAX(c.customer_email) AS email, SUM(o.order_total) AS total
                               FROM orders o LEFT JOIN customers c ON o.customer_id = c.customer_id
                               WHERE o.order_id = %s GROUP BY o.order_id""", (oid,))
                r = cur.fetchone()
            if not r:
                continue
            base = {"customer": r.get("customer") or "", "email": r.get("email") or "",
                    "total_display": f"\u20b9{float(r['total'] or 0):.0f}",
                    "total_inr": float(r["total"] or 0), "telephone": ""}
        if search:
            s = search.strip().upper()
            if s not in oid and s not in (base["customer"] or "").upper() \
                    and s not in (base["email"] or "").upper():
                continue
        out.append({
            "order_id": oid, "source": "optiwar", "reship": True,
            "display_id": reship_svc.next_reference(oid),
            "customer": base["customer"], "email": base["email"],
            "telephone": base.get("telephone", ""),
            "total_display": "\u20b9250 reship fee", "total_inr": 250.0,
            "status_name": "Reship PAID", "order_status_id": "reship",
            "date_added": e.get("paid_at") or "",
            "original_awb": e.get("original_awb") or "",
            "original_courier": e.get("original_courier") or "",
            "link": f"orders/ow/{oid}",
            "ship_link": f"orders/ow/{oid}/ship?courier=DTDC&reship=1",
        })
    return out


def list_processing_orders(page: int = 1, per_page: int = 30, search: str = None,
                           source_filter: str = None, status_filter: str = None):
    """Combine orders from EU (oslb6) and Optiwar (optiwar2) into one list sorted by date."""
    from services.optiwar_orders import OW_STATUS_MAP, LATEST_STATUS_SQL

    eu_orders = []
    ow_orders_list = []

    # --- EU orders ---
    if source_filter in (None, "", "all", "eu"):
        # "Missing" = orders stuck at order_status_id = 0 (checkout/payment never
        # confirmed, so no status was assigned). Only surfaced when explicitly
        # requested; the default list still excludes them (order_status_id > 0).
        is_missing = (status_filter or "").strip().lower() == "missing"
        eu_where = ["o.order_status_id = 0" if is_missing else "o.order_status_id > 0",
                    "o.store_id = 0"]
        eu_params = []
        if search:
            s = search.strip()
            if s.upper().startswith("E-"):
                s = s[2:]
            eu_where.append(
                "(o.order_id = %s OR o.firstname LIKE %s OR o.lastname LIKE %s OR o.email LIKE %s)")
            eu_params.extend([s, f"%{s}%", f"%{s}%", f"%{s}%"])
        if not is_missing and status_filter and status_filter not in ("all", ""):
            try:
                sid = int(status_filter)
                eu_where.append("o.order_status_id = %s")
                eu_params.append(sid)
            except ValueError:
                eu_where.append("os.name = %s")
                eu_params.append(status_filter)
        eu_sql = " AND ".join(eu_where)
        with db_cursor() as cur:
            cur.execute(f"""
                SELECT o.order_id, o.firstname, o.lastname, o.email, o.telephone,
                       o.total, o.currency_value, o.order_status_id, o.date_added,
                       os.name as status_name
                FROM `order` o
                LEFT JOIN order_status os ON o.order_status_id = os.order_status_id AND os.language_id = 1
                WHERE {eu_sql}
                ORDER BY o.date_added DESC
                LIMIT 500
            """, eu_params)
            rows = cur.fetchall()
        for r in rows:
            eur_rate = float(r.get("currency_value") or 0.0108)
            eu_orders.append({
                "order_id": r["order_id"],
                "source": "eu",
                "display_id": f"E-{r['order_id']}",
                "customer": f"{r['firstname']} {r['lastname']}".strip(),
                "email": r["email"],
                "telephone": r.get("telephone", ""),
                "total_display": f"\u20ac{float(r['total']) * eur_rate:.2f}",
                "total_inr": float(r["total"]),
                "status_name": r["status_name"] or ("Missing" if r["order_status_id"] == 0 else ""),
                "order_status_id": r["order_status_id"],
                "date_added": r["date_added"],
                "link": f"orders/{r['order_id']}",
                "ship_link": f"orders/{r['order_id']}/ship",
            })

    # --- Optiwar orders ---
    if source_filter in (None, "", "all", "optiwar"):
        ow_where = ["o.archived = 0"]
        ow_params = []
        if search:
            s = search.strip()
            if s.upper().startswith("OW-"):
                s = s[3:]
            ow_where.append(
                "(o.order_id LIKE %s OR c.customer_name LIKE %s OR c.customer_email LIKE %s)")
            ow_params.extend([f"%{s}%", f"%{s}%", f"%{s}%"])
        # Unpaid orders are hidden from the default queue, but not when they are
        # what was asked for: filtering Pending used to AND with != 'Pending'
        # and return nothing at all.
        if status_filter and status_filter not in ("all", ""):
            ow_where.append("os_agg.order_status_name = %s")
            ow_params.append(status_filter)
        else:
            ow_where.append("os_agg.order_status_name != 'Pending'")
        ow_sql = " AND ".join(ow_where)
        with ow_cursor() as cur:
            cur.execute(f"""
                SELECT o.order_id,
                       MAX(o.date_created) as date_added,
                       SUM(o.order_total) as total,
                       MAX(c.customer_name) as firstname,
                       MAX(c.customer_email) as email,
                       MAX(c.customer_phone) as telephone,
                       MAX(os_agg.order_status_name) as status_name
                FROM orders o
                LEFT JOIN customers c ON o.customer_id = c.customer_id
                LEFT JOIN ({LATEST_STATUS_SQL}) os_agg ON o.order_id = os_agg.order_id
                WHERE {ow_sql}
                GROUP BY o.order_id
                ORDER BY MAX(o.date_created) DESC
                LIMIT 500
            """, ow_params)
            rows = cur.fetchall()
        for r in rows:
            sname = r.get("status_name") or ""
            ow_orders_list.append({
                "order_id": r["order_id"],
                "source": "optiwar",
                "display_id": f"OW-{r['order_id']}",
                "customer": r.get("firstname") or "",
                "email": r.get("email") or "",
                "telephone": str(r.get("telephone") or ""),
                "total_display": f"\u20b9{float(r['total'] or 0):.0f}",
                "total_inr": float(r["total"] or 0),
                "status_name": sname,
                "order_status_id": OW_STATUS_MAP.get(sname, 0),
                "date_added": r["date_added"],
                "link": f"orders/ow/{r['order_id']}",
                "ship_link": f"orders/ow/{r['order_id']}/ship",
            })

    # Merge and sort by date descending
    combined = eu_orders + ow_orders_list
    combined.sort(key=lambda x: str(x.get("date_added") or ""), reverse=True)

    # Orders in Optiwar's return lifecycle show the live state (not a bare
    # 'Returned'); PAID reships are work waiting on Ops and are pinned on top.
    reships = []
    if source_filter in (None, "", "all", "optiwar"):
        from services import reship as reship_svc
        lifecycle = reship_svc.lifecycle_map() if reship_svc.configured() else {}
        for o in ow_orders_list:
            info = lifecycle.get(str(o["order_id"]).upper())
            if info:
                o["ow_return"] = info
                if info["paid"]:
                    o["ship_link"] = f"orders/ow/{o['order_id']}/ship?courier=DTDC&reship=1"
                elif info["status"] == "RETURNING":
                    o["ship_link"] = ""
                    o["receive_link"] = f"orders/ow/{o['order_id']}#return-receipt"
                else:
                    o["ship_link"] = ""
        if not status_filter:
            reships = _paid_reship_rows(ow_orders_list, search, lifecycle)
    combined = reships + combined

    total = len(combined)
    start = (page - 1) * per_page
    page_orders = combined[start:start + per_page]

    return {
        "orders": page_orders,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page if total else 1,
        "eu_count": len(eu_orders),
        "ow_count": len(ow_orders_list),
    }
