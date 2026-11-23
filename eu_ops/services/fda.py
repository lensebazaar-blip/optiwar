"""FDA / US Customs manufacturer details, keyed by product_id.

Multiple records per product_id are allowed (same product manufactured in
different countries). For an order line, the applicable record is resolved as:
  - explicit choice stored in order_fda_map, else
  - the single active record for the product, else
  - None (missing -> prompt the operator to enter it).
"""
from database import db_cursor


def list_all():
    with db_cursor() as cur:
        cur.execute("""
            SELECT * FROM fda_details
            ORDER BY product_name, manufacturer_country, id
        """)
        return cur.fetchall()


def get(fda_id):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM fda_details WHERE id = %s", (fda_id,))
        return cur.fetchone()


def list_for_product(product_id, active_only=True):
    sql = "SELECT * FROM fda_details WHERE product_id = %s"
    if active_only:
        sql += " AND is_active = 1"
    sql += " ORDER BY manufacturer_country, id"
    with db_cursor() as cur:
        cur.execute(sql, (product_id,))
        return cur.fetchall()


def add(product_id, product_name, manufacturer_name, manufacturer_country,
        manufacturer_address, registration_number, fda_510k_number,
        pma_number, classification_product_code, created_by):
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO fda_details
              (product_id, product_name, manufacturer_name, manufacturer_country,
               manufacturer_address, registration_number, fda_510k_number,
               pma_number, classification_product_code, is_active, created_by, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,NOW())
        """, (product_id, product_name, manufacturer_name, manufacturer_country,
              manufacturer_address, registration_number, fda_510k_number,
              pma_number, classification_product_code, created_by))
        return cur.lastrowid


def update(fda_id, manufacturer_name, manufacturer_country, manufacturer_address,
           registration_number, fda_510k_number, pma_number,
           classification_product_code, is_active, modified_by):
    with db_cursor() as cur:
        cur.execute("""
            UPDATE fda_details SET
              manufacturer_name = %s,
              manufacturer_country = %s,
              manufacturer_address = %s,
              registration_number = %s,
              fda_510k_number = %s,
              pma_number = %s,
              classification_product_code = %s,
              is_active = %s,
              modified_by = %s,
              modified_at = NOW()
            WHERE id = %s
        """, (manufacturer_name, manufacturer_country, manufacturer_address,
              registration_number, fda_510k_number, pma_number,
              classification_product_code, is_active, modified_by, fda_id))


def get_map(order_id):
    """Return {order_product_id: fda_id} for explicit per-line choices."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT order_product_id, fda_id FROM order_fda_map WHERE order_id = %s",
            (order_id,))
        return {r["order_product_id"]: r["fda_id"] for r in cur.fetchall()}


def set_map(order_id, order_product_id, fda_id, set_by):
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO order_fda_map (order_id, order_product_id, fda_id, set_by, set_at)
            VALUES (%s,%s,%s,%s,NOW())
            ON DUPLICATE KEY UPDATE fda_id = VALUES(fda_id), set_by = VALUES(set_by), set_at = NOW()
        """, (order_id, order_product_id, fda_id, set_by))


def resolve_for_order(order_id, products):
    """For each order product line, resolve its FDA record.

    Returns (fda_lines, missing, choices):
      fda_lines: list of dicts {product line + 'fda': record or None}
      missing:   product lines with no active FDA record at all
      choices:   product lines that have >1 active record and no explicit map
    """
    chosen = get_map(order_id)
    fda_lines, missing, choices = [], [], []
    for p in products:
        pid = p["product_id"]
        op_id = p["order_product_id"]
        records = list_for_product(pid, active_only=True)
        rec = None
        if op_id in chosen:
            rec = next((r for r in records if r["id"] == chosen[op_id]), None)
            if rec is None:
                rec = get(chosen[op_id])
        if rec is None:
            if len(records) == 1:
                rec = records[0]
            elif len(records) > 1:
                choices.append({"line": p, "options": records})
        if rec is None and not any(c["line"]["order_product_id"] == op_id for c in choices):
            missing.append(p)
        fda_lines.append({"line": p, "fda": rec})
    return fda_lines, missing, choices
