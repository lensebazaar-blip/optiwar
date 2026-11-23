"""Shipped-orders report service — EU (oslb6, OpenCart) + Optiwar (optiwar2)."""
import csv
import io
from datetime import datetime, timedelta

from database import db_cursor, ow_cursor

EU_SHIPPED_STATUS_ID = 3  # OpenCart "Shipped"

SITE_LABELS = {"eu": "EU LensBazaar", "ow": "Optiwar"}

_EU_SQL = """
SELECT
  o.order_id,
  CONCAT_WS(' ', NULLIF(o.firstname,''), NULLIF(o.lastname,'')) AS customer_name,
  o.telephone,
  o.shipping_country,
  o.shipping_zone,
  shipped.first_shipped_at            AS shipped_at,
  os.name                             AS order_status_name,
  COALESCE(NULLIF(awb.awb_tracking, ''),
           NULLIF(o.awbno, 'NOT GENERATED'), '') AS awbno,
  op.order_product_id,
  op.name                             AS product_name,
  op.model                            AS product_model,
  op.quantity                         AS product_quantity,
  opt.options_text
FROM `order` o
JOIN (
  SELECT order_id, MIN(date_added) AS first_shipped_at
  FROM order_history
  WHERE order_status_id = %(shipped_status)s
  GROUP BY order_id
) shipped ON shipped.order_id = o.order_id
LEFT JOIN order_status os ON os.order_status_id = o.order_status_id
LEFT JOIN (
  SELECT order_id,
         SUBSTRING_INDEX(
           GROUP_CONCAT(tracking_number
             ORDER BY (awb_status = 'cancelled') ASC, id DESC),
           ',', 1) AS awb_tracking
  FROM shipping_awb
  WHERE tracking_number IS NOT NULL AND tracking_number <> ''
  GROUP BY order_id
) awb ON awb.order_id = o.order_id
JOIN order_product op ON op.order_id = o.order_id
LEFT JOIN (
  SELECT order_product_id,
         GROUP_CONCAT(CONCAT(name, ': ', value) SEPARATOR ' | ') AS options_text
  FROM order_option
  GROUP BY order_product_id
) opt ON opt.order_product_id = op.order_product_id
WHERE shipped.first_shipped_at >= %(from_dt)s
  AND shipped.first_shipped_at <  %(to_dt)s
ORDER BY shipped.first_shipped_at DESC, o.order_id, op.order_product_id
"""

_OW_SQL = """
SELECT
  o.order_id,
  c.customer_name                     AS customer_name,
  c.customer_phone                    AS telephone,
  ca.country                          AS shipping_country,
  ca.state                            AS shipping_zone,
  shipped.first_shipped_at            AS shipped_at,
  'Shipped'                           AS order_status_name,
  ''                                  AS awbno,
  o.order_line_id                     AS order_product_id,
  p.product_name                      AS product_name,
  p.product_code                      AS product_model,
  o.order_quantity                    AS product_quantity,
  NULL                                AS options_text
FROM orders o
JOIN (
  SELECT order_id, MIN(order_history_date) AS first_shipped_at
  FROM order_history
  WHERE order_history_content = 'Status updated to Shipped'
  GROUP BY order_id
) shipped ON shipped.order_id = o.order_id
LEFT JOIN customers c ON c.customer_id = o.customer_id
LEFT JOIN products  p ON p.product_id  = o.product_id
LEFT JOIN (
  SELECT customer_id, MAX(address_id) AS aid
  FROM customers_address GROUP BY customer_id
) ca_pick ON ca_pick.customer_id = o.customer_id
LEFT JOIN customers_address ca ON ca.address_id = ca_pick.aid
WHERE shipped.first_shipped_at >= %(from_dt)s
  AND shipped.first_shipped_at <  %(to_dt)s
ORDER BY shipped.first_shipped_at DESC, o.order_id, o.order_line_id
"""

COLUMNS = [
    ("display_order_id", "Order ID"),
    ("customer_name", "Customer"),
    ("telephone", "Phone"),
    ("shipping_country", "Country"),
    ("shipping_zone", "State/Zone"),
    ("shipped_at", "Shipped At"),
    ("product_name", "Product"),
    ("product_model", "Model"),
    ("product_quantity", "Qty"),
    ("options_text", "Options / Rx"),
    ("awbno", "AWB"),
    ("source_label", "Site"),
]


def _parse_range(from_date: str, to_date: str):
    """Return (from_dt, to_dt) — to_dt is exclusive (end date + 1 day)."""
    from_dt = datetime.strptime(from_date, "%Y-%m-%d")
    to_dt = datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)
    return from_dt, to_dt


def _fetch_eu(from_dt, to_dt):
    with db_cursor() as cur:
        cur.execute(_EU_SQL, {
            "shipped_status": EU_SHIPPED_STATUS_ID,
            "from_dt": from_dt, "to_dt": to_dt,
        })
        rows = cur.fetchall()
    for r in rows:
        r["source"] = "eu"
        r["source_label"] = SITE_LABELS["eu"]
        r["display_order_id"] = f"E-{r['order_id']}"
    return rows


def _fetch_ow(from_dt, to_dt):
    try:
        with ow_cursor() as cur:
            cur.execute(_OW_SQL, {"from_dt": from_dt, "to_dt": to_dt})
            rows = cur.fetchall()
    except Exception:
        return []
    for r in rows:
        r["source"] = "ow"
        r["source_label"] = SITE_LABELS["ow"]
        r["display_order_id"] = f"OW-{r['order_id']}"
    return rows


def get_shipped_report(site: str, from_date: str, to_date: str):
    """site: 'all' | 'eu' | 'ow'. Returns dict with rows + summary."""
    from_dt, to_dt = _parse_range(from_date, to_date)
    rows = []
    if site in ("all", "eu"):
        rows += _fetch_eu(from_dt, to_dt)
    if site in ("all", "ow"):
        rows += _fetch_ow(from_dt, to_dt)

    if site == "all":
        rows.sort(key=lambda r: (r["shipped_at"] or datetime.min), reverse=True)

    for r in rows:
        if isinstance(r.get("shipped_at"), datetime):
            r["shipped_at"] = r["shipped_at"].strftime("%Y-%m-%d %H:%M")

    order_keys = {r["display_order_id"] for r in rows}
    eu_orders = {r["display_order_id"] for r in rows if r["source"] == "eu"}
    ow_orders = {r["display_order_id"] for r in rows if r["source"] == "ow"}
    total_qty = sum(int(r.get("product_quantity") or 0) for r in rows)

    summary = {
        "total_orders": len(order_keys),
        "total_lines": len(rows),
        "total_qty": total_qty,
        "eu_orders": len(eu_orders),
        "ow_orders": len(ow_orders),
    }
    return {"rows": rows, "summary": summary}


def build_csv(rows) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _, label in COLUMNS])
    for r in rows:
        writer.writerow([r.get(key, "") if r.get(key) is not None else "" for key, _ in COLUMNS])
    return buf.getvalue()


def build_pdf(rows, summary, site_label, from_date, to_date) -> bytes:
    """Grouped-by-order PDF using reportlab."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm)
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, leading=10)
    hdr = ParagraphStyle("hdr", parent=styles["Normal"], fontSize=9,
                         leading=11, textColor=colors.HexColor("#1e293b"),
                         spaceBefore=6)

    elements = []
    elements.append(Paragraph("Shipped Orders Report", title_style))
    elements.append(Paragraph(
        f"Site: <b>{site_label}</b> &nbsp;|&nbsp; Range: <b>{from_date}</b> to <b>{to_date}</b> "
        f"(by first-shipped date)", small))
    elements.append(Paragraph(
        f"Orders: <b>{summary['total_orders']}</b> &nbsp;|&nbsp; Line items: "
        f"<b>{summary['total_lines']}</b> &nbsp;|&nbsp; Total qty: <b>{summary['total_qty']}</b> "
        f"&nbsp;|&nbsp; EU: {summary['eu_orders']} &nbsp; Optiwar: {summary['ow_orders']}", small))
    elements.append(Spacer(1, 6))

    # group rows by order
    grouped = {}
    order_meta = {}
    order_sequence = []
    for r in rows:
        oid = r["display_order_id"]
        if oid not in grouped:
            grouped[oid] = []
            order_sequence.append(oid)
            order_meta[oid] = r
        grouped[oid].append(r)

    prod_header = ["Product", "Model", "Qty", "Options / Rx"]
    for oid in order_sequence:
        meta = order_meta[oid]
        elements.append(Paragraph(
            f"<b>{oid}</b> &nbsp; {meta.get('customer_name') or ''} &nbsp;|&nbsp; "
            f"{meta.get('shipping_country') or ''} {('- ' + meta.get('shipping_zone')) if meta.get('shipping_zone') else ''} "
            f"&nbsp;|&nbsp; Shipped: {meta.get('shipped_at') or ''} "
            f"&nbsp;|&nbsp; {meta.get('source_label')}"
            + (f" &nbsp;|&nbsp; AWB: {meta.get('awbno')}" if meta.get('awbno') else ""),
            hdr))
        data = [prod_header]
        for r in grouped[oid]:
            data.append([
                Paragraph(str(r.get("product_name") or ""), small),
                Paragraph(str(r.get("product_model") or ""), small),
                str(r.get("product_quantity") or ""),
                Paragraph(str(r.get("options_text") or ""), small),
            ])
        t = Table(data, colWidths=[90 * mm, 45 * mm, 12 * mm, 110 * mm], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563eb")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 8))

    if not order_sequence:
        elements.append(Paragraph("No shipped orders found for this range.", small))

    doc.build(elements)
    return buf.getvalue()
