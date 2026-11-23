"""Merchant Center service — product management for Google Merchant feeds."""
from database import db_cursor

# Categories to exclude from Merchant Center (not sold as contact lenses)
EXCLUDED_CATEGORIES = ('Sunglasses', 'Upsell Category (do not delete)', 'Lens Solutions')


def _excluded_subquery():
    """Subquery to get product IDs that belong to excluded categories."""
    placeholders = ",".join(["%s"] * len(EXCLUDED_CATEGORIES))
    return f"""p.product_id NOT IN (
        SELECT ptc_ex.product_id FROM product_to_category ptc_ex
        JOIN category_description cd_ex ON ptc_ex.category_id = cd_ex.category_id
        WHERE cd_ex.name IN ({placeholders})
    )"""


def list_products(search: str = "", category_filter: str = "", eu_filter: str = ""):
    """List active contact lens products with categories, eu_saleable flag, and status."""
    with db_cursor() as cur:
        where = ["p.status = 1", _excluded_subquery()]
        params = list(EXCLUDED_CATEGORIES)

        if search:
            where.append("(pd.name LIKE %s OR p.product_id = %s)")
            params.extend([f"%{search}%", search])

        if category_filter:
            where.append("""p.product_id IN (
                SELECT ptc2.product_id FROM product_to_category ptc2
                JOIN category_description cd2 ON ptc2.category_id = cd2.category_id
                WHERE cd2.name = %s
            )""")
            params.append(category_filter)

        if eu_filter == "1":
            where.append("p.eu_saleable = 1")
        elif eu_filter == "0":
            where.append("p.eu_saleable = 0")

        sql = f"""
            SELECT p.product_id, pd.name AS product_name, p.status, p.eu_saleable,
                   p.price, p.quantity, p.model, p.sku,
                   COALESCE(m.name, '') AS manufacturer,
                   GROUP_CONCAT(DISTINCT cd.name ORDER BY cd.name SEPARATOR ', ') AS categories,
                   (SELECT keyword FROM url_alias WHERE query = CONCAT('product_id=', p.product_id) LIMIT 1) AS seo_url,
                   p.image
            FROM product p
            JOIN product_description pd ON p.product_id = pd.product_id
            JOIN product_to_category ptc ON p.product_id = ptc.product_id
            JOIN category_description cd ON ptc.category_id = cd.category_id
            LEFT JOIN manufacturer m ON p.manufacturer_id = m.manufacturer_id
            WHERE {' AND '.join(where)}
            GROUP BY p.product_id, pd.name, p.status, p.eu_saleable, p.price,
                     p.quantity, p.model, p.sku, m.name, p.image
            ORDER BY categories, pd.name
        """
        cur.execute(sql, params)
        return cur.fetchall()


def get_categories():
    """Get distinct category names for the filter dropdown (excluding non-lens categories)."""
    with db_cursor() as cur:
        placeholders = ",".join(["%s"] * len(EXCLUDED_CATEGORIES))
        cur.execute(f"""
            SELECT DISTINCT cd.name
            FROM category_description cd
            JOIN product_to_category ptc ON cd.category_id = ptc.category_id
            JOIN product p ON ptc.product_id = p.product_id AND p.status = 1
            WHERE cd.name NOT IN ({placeholders})
            AND p.product_id NOT IN (
                SELECT ptc_ex.product_id FROM product_to_category ptc_ex
                JOIN category_description cd_ex ON ptc_ex.category_id = cd_ex.category_id
                WHERE cd_ex.name IN ({placeholders})
            )
            ORDER BY cd.name
        """, EXCLUDED_CATEGORIES + EXCLUDED_CATEGORIES)
        return [row["name"] for row in cur.fetchall()]


def toggle_eu_saleable(product_id: int, value: int):
    """Set eu_saleable flag for a product."""
    with db_cursor() as cur:
        cur.execute("UPDATE product SET eu_saleable = %s WHERE product_id = %s",
                     (value, product_id))
        return cur.rowcount > 0


def toggle_product_status(product_id: int, value: int):
    """Enable or disable a product (status 1/0)."""
    with db_cursor() as cur:
        cur.execute("UPDATE product SET status = %s WHERE product_id = %s",
                     (value, product_id))
        return cur.rowcount > 0


def bulk_set_eu_saleable(product_ids: list, value: int):
    """Set eu_saleable for multiple products at once."""
    if not product_ids:
        return 0
    with db_cursor() as cur:
        placeholders = ",".join(["%s"] * len(product_ids))
        cur.execute(
            f"UPDATE product SET eu_saleable = %s WHERE product_id IN ({placeholders})",
            [value] + product_ids
        )
        return cur.rowcount


def get_product_stats():
    """Get summary stats for contact lens products only (excluding sunglasses/solutions/upsell)."""
    with db_cursor() as cur:
        placeholders = ",".join(["%s"] * len(EXCLUDED_CATEGORIES))
        cur.execute(f"""
            SELECT
                COUNT(*) as total,
                SUM(p.status = 1) as active,
                SUM(p.status = 0) as disabled,
                SUM(p.status = 1 AND p.eu_saleable = 1) as eu_saleable,
                SUM(p.status = 1 AND p.eu_saleable = 0) as eu_restricted
            FROM product p
            WHERE p.product_id NOT IN (
                SELECT ptc_ex.product_id FROM product_to_category ptc_ex
                JOIN category_description cd_ex ON ptc_ex.category_id = cd_ex.category_id
                WHERE cd_ex.name IN ({placeholders})
            )
        """, EXCLUDED_CATEGORIES)
        return cur.fetchone()
