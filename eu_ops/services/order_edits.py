"""Order editing service — partial shipment, quantity adjustment, refund tracking."""
import json
import logging
import re
from datetime import datetime
from database import db_cursor, db_transaction

log = logging.getLogger("order_edits")


def get_razorpay_payment_id(order_id: int) -> str:
    """Extract Razorpay payment_id from order_history comment."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT comment FROM order_history
            WHERE order_id = %s AND comment LIKE '%%Razorpay Payment ID%%'
            ORDER BY date_added ASC LIMIT 1
        """, (order_id,))
        row = cur.fetchone()
    if not row:
        return ""
    m = re.search(r"Razorpay Payment ID:\s*(pay_\w+)", row["comment"])
    return m.group(1) if m else ""


def get_order_edits(order_id: int) -> list:
    """Return all edits for an order."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT * FROM order_edits
            WHERE order_id = %s
            ORDER BY created_at DESC
        """, (order_id,))
        return cur.fetchall()


def get_pending_refund(order_id: int) -> dict | None:
    """Get the latest pending refund for an order."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT * FROM order_edits
            WHERE order_id = %s AND refund_status = 'pending'
            ORDER BY created_at DESC LIMIT 1
        """, (order_id,))
        return cur.fetchone()


def calculate_refund(order_id: int, new_quantities: dict) -> dict:
    """Calculate refund amount based on quantity changes.

    new_quantities: {order_option_id: new_qty_value} for Quantity options only.
    Returns dict with original/new totals, refund amount in EUR, changed items.
    """
    with db_cursor() as cur:
        # Get order with EUR rate
        cur.execute("""
            SELECT o.total, o.currency_value
            FROM `order` o WHERE o.order_id = %s
        """, (order_id,))
        order_row = cur.fetchone()
        if not order_row:
            raise ValueError("Order not found")

        eur_rate = float(order_row.get("currency_value") or 0.0108)

        # Get products
        cur.execute("""
            SELECT op.order_product_id, op.product_id, op.name, op.model,
                   op.quantity, op.price, op.total
            FROM order_product op WHERE op.order_id = %s
        """, (order_id,))
        products = cur.fetchall()

        changes = []
        total_refund_inr = 0.0

        for prod in products:
            cur.execute("""
                SELECT order_option_id, name, value, type
                FROM order_option
                WHERE order_product_id = %s
                ORDER BY order_option_id
            """, (prod["order_product_id"],))
            options = cur.fetchall()

            # Find Quantity options and check for changes
            unit_price = float(prod["price"])
            current_eye = None

            for opt in options:
                name_upper = opt["name"].strip().upper()
                if "LEFT" in name_upper:
                    current_eye = "LEFT"
                elif "RIGHT" in name_upper:
                    current_eye = "RIGHT"

                opt_id_str = str(opt["order_option_id"])
                if opt["name"].strip().lower() == "quantity" and opt_id_str in new_quantities:
                    old_qty = int(opt["value"])
                    new_qty = int(new_quantities[opt_id_str])
                    if new_qty < 0:
                        raise ValueError(f"Quantity cannot be negative")
                    if new_qty > old_qty:
                        raise ValueError(f"New quantity ({new_qty}) cannot exceed original ({old_qty})")
                    if new_qty != old_qty:
                        qty_diff = old_qty - new_qty
                        refund_inr = qty_diff * unit_price
                        total_refund_inr += refund_inr
                        changes.append({
                            "product_name": prod["name"],
                            "eye": current_eye or "N/A",
                            "order_option_id": opt["order_option_id"],
                            "order_product_id": prod["order_product_id"],
                            "old_qty": old_qty,
                            "new_qty": new_qty,
                            "qty_removed": qty_diff,
                            "unit_price_inr": unit_price,
                            "refund_inr": round(refund_inr, 2),
                            "refund_eur": round(refund_inr * eur_rate, 2),
                        })

    refund_eur = round(total_refund_inr * eur_rate, 2)

    return {
        "order_id": order_id,
        "changes": changes,
        "total_refund_inr": round(total_refund_inr, 2),
        "total_refund_eur": refund_eur,
        "eur_rate": eur_rate,
        "razorpay_payment_id": get_razorpay_payment_id(order_id),
    }


def save_order_edit(order_id: int, changes: list, refund_eur: float,
                    refund_inr: float, razorpay_payment_id: str,
                    edited_by: int, notes: str = "") -> int:
    """Save order edit and update the order_option quantities in the DB.
    Returns the edit record ID."""
    with db_cursor() as cur:
        # Save edit record
        cur.execute("""
            INSERT INTO order_edits
                (order_id, edit_data, refund_amount_eur, refund_amount_inr,
                 razorpay_payment_id, refund_status, edited_by, notes, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (order_id, json.dumps(changes), refund_eur, refund_inr,
              razorpay_payment_id, "pending" if refund_eur > 0 else "none",
              edited_by, notes))
        edit_id = cur.lastrowid

        # Update order_option quantities
        for change in changes:
            cur.execute("""
                UPDATE order_option SET value = %s
                WHERE order_option_id = %s
            """, (str(change["new_qty"]), change["order_option_id"]))

        # Update order_product quantity and total
        product_updates = {}
        for change in changes:
            pid = change["order_product_id"]
            if pid not in product_updates:
                product_updates[pid] = {"qty_removed": 0, "refund_inr": 0}
            product_updates[pid]["qty_removed"] += change["qty_removed"]
            product_updates[pid]["refund_inr"] += change["refund_inr"]

        for pid, upd in product_updates.items():
            cur.execute("""
                UPDATE order_product
                SET quantity = quantity - %s,
                    total = total - %s
                WHERE order_product_id = %s
            """, (upd["qty_removed"], upd["refund_inr"], pid))

        # Update order total
        if refund_inr > 0:
            cur.execute("""
                UPDATE `order` SET total = total - %s WHERE order_id = %s
            """, (refund_inr, order_id))
            # Update order_total sub_total and total rows (value AND text)
            cur.execute("""
                UPDATE order_total SET value = value - %s
                WHERE order_id = %s AND code IN ('sub_total', 'total')
            """, (refund_inr, order_id))
            # Recalculate EUR text from updated INR values
            cur.execute("""
                SELECT o.currency_value FROM `order` o WHERE o.order_id = %s
            """, (order_id,))
            cv = float(cur.fetchone()["currency_value"] or 0.0108)
            cur.execute("""
                SELECT order_total_id, code, value FROM order_total
                WHERE order_id = %s AND code IN ('sub_total', 'total')
            """, (order_id,))
            for row in cur.fetchall():
                new_eur = round(float(row["value"]) * cv, 2)
                cur.execute("""
                    UPDATE order_total SET text = %s
                    WHERE order_total_id = %s
                """, (f"€{new_eur:.2f}", row["order_total_id"]))

        return edit_id


def mark_refund_completed(edit_id: int, refund_reference: str = ""):
    """Mark a pending refund as completed (agent did it manually in Razorpay)."""
    with db_cursor() as cur:
        cur.execute("""
            UPDATE order_edits
            SET refund_status = 'completed', refund_reference = %s,
                refunded_at = NOW()
            WHERE id = %s
        """, (refund_reference, edit_id))


# ─── EU refund calculator (Razorpay settles in INR) ──────────────────

def compute_refund_amounts(order: dict, refund_type: str = "full",
                           rate: float = 0.0, deduction_return_inr: float = 0.0,
                           deduction_freight_inr: float = 0.0) -> dict:
    """Compute an EU refund. Razorpay refunds in INR, so the figure we push is INR;
    the customer-facing figure is in EUR.

    Full   : refund the entire amount paid.
    Partial: paid(EUR) x rate(INR/EUR) - return-goods cost - freight/other charges,
             then converted back to EUR for the customer.
    """
    paid_eur = round(float(order.get("total_eur") or 0.0), 2)
    paid_inr = round(float(order.get("total_inr") or order.get("total") or 0.0), 2)
    if rate <= 0:
        rate = round(paid_inr / paid_eur, 4) if paid_eur else 0.0

    if refund_type == "full":
        deduction_return_inr = 0.0
        deduction_freight_inr = 0.0
        base_inr = paid_inr
        refund_inr = paid_inr
        refund_eur = paid_eur
    else:
        base_inr = round(paid_eur * rate, 2)
        refund_inr = round(base_inr - float(deduction_return_inr) - float(deduction_freight_inr), 2)
        if refund_inr < 0:
            refund_inr = 0.0
        refund_eur = round(refund_inr / rate, 2) if rate else 0.0

    ded_return_eur = round(float(deduction_return_inr) / rate, 2) if rate else 0.0
    ded_freight_eur = round(float(deduction_freight_inr) / rate, 2) if rate else 0.0

    return {
        "refund_type": refund_type,
        "paid_eur": paid_eur,
        "paid_inr": paid_inr,
        "rate": rate,
        "base_inr": base_inr,
        "deduction_return_inr": round(float(deduction_return_inr), 2),
        "deduction_freight_inr": round(float(deduction_freight_inr), 2),
        "deduction_return_eur": ded_return_eur,
        "deduction_freight_eur": ded_freight_eur,
        "refund_inr": refund_inr,
        "refund_eur": refund_eur,
        "razorpay_payment_id": get_razorpay_payment_id(order["order_id"]),
    }


def save_refund(order_id: int, calc: dict, reason: str, edited_by: int) -> int:
    """Insert a money-only refund into order_edits (no quantity mutation). It starts
    'pending' and is closed when the operator enters the Razorpay refund reference
    via the existing Mark Refund Completed flow."""
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO order_edits
                (order_id, edit_data, refund_amount_eur, refund_amount_inr,
                 razorpay_payment_id, refund_status, edited_by, notes, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (order_id, json.dumps({"refund": calc, "reason": reason}),
              calc["refund_eur"], calc["refund_inr"],
              calc.get("razorpay_payment_id", ""),
              "pending" if calc["refund_eur"] > 0 else "none",
              edited_by, reason))
        return cur.lastrowid


def _refund_breakdown_html(calc: dict) -> str:
    """CUSTOMER-facing refund breakdown table — EUR only (no INR). Never AI-generated
    so the figures are always accurate."""
    cell = 'style="padding:6px 8px;border:1px solid #e2e8f0;"'
    cellr = 'style="padding:6px 8px;border:1px solid #e2e8f0;text-align:right;"'
    rows = [f'<tr><td {cell}>Amount paid</td><td {cellr}>&euro;{calc["paid_eur"]:.2f}</td></tr>']
    if calc["refund_type"] != "full":
        rows.append(f'<tr><td {cell}>Less: return goods processing</td>'
                    f'<td {cellr}>&euro;{calc["deduction_return_eur"]:.2f}</td></tr>')
        rows.append(f'<tr><td {cell}>Less: freight / other charges</td>'
                    f'<td {cellr}>&euro;{calc["deduction_freight_eur"]:.2f}</td></tr>')
    rows.append(f'<tr style="font-weight:700;background:#f0fdf4;"><td {cell}>Refund</td>'
                f'<td {cellr}>&euro;{calc["refund_eur"]:.2f}</td></tr>')
    return ('<table style="width:100%;border-collapse:collapse;margin:12px 0;font-size:14px;">'
            + "".join(rows) + '</table>')


def build_refund_admin_email(order: dict, calc: dict, reason: str = "",
                             refunded_by: str = "") -> dict:
    """INTERNAL refund email for admin@lensbazaar.com — full conversion detail
    (INR base, INR deductions, INR pushed to Razorpay) alongside the EUR equivalents,
    so the team can book the refund correctly."""
    order_id = order["order_id"]
    customer = f"{order.get('firstname','')} {order.get('lastname','')}".strip()
    cell = 'style="padding:6px 8px;border:1px solid #e2e8f0;"'
    cellr = 'style="padding:6px 8px;border:1px solid #e2e8f0;text-align:right;"'
    rows = [f'<tr><td {cell}>Amount paid</td><td {cellr}>&euro;{calc["paid_eur"]:.2f}</td>'
            f'<td {cellr}>&#8377;{calc["paid_inr"]:.2f}</td></tr>']
    if calc["refund_type"] != "full":
        rows.append(f'<tr><td {cell}>Converted at &#8377;{calc["rate"]:.2f}/&euro;</td>'
                    f'<td {cellr}>&euro;{calc["paid_eur"]:.2f}</td>'
                    f'<td {cellr}>&#8377;{calc["base_inr"]:.2f}</td></tr>')
        rows.append(f'<tr><td {cell}>Less: return goods processing</td>'
                    f'<td {cellr}>&euro;{calc["deduction_return_eur"]:.2f}</td>'
                    f'<td {cellr}>&#8377;{calc["deduction_return_inr"]:.2f}</td></tr>')
        rows.append(f'<tr><td {cell}>Less: freight / other charges</td>'
                    f'<td {cellr}>&euro;{calc["deduction_freight_eur"]:.2f}</td>'
                    f'<td {cellr}>&#8377;{calc["deduction_freight_inr"]:.2f}</td></tr>')
    rows.append(f'<tr style="font-weight:700;background:#f0fdf4;"><td {cell}>Refund</td>'
                f'<td {cellr}>&euro;{calc["refund_eur"]:.2f}</td>'
                f'<td {cellr}>&#8377;{calc["refund_inr"]:.2f}</td></tr>')
    table = ('<table style="width:100%;border-collapse:collapse;margin:12px 0;font-size:14px;">'
             f'<tr style="background:#f1f5f9;"><th {cell}>Line</th><th {cellr}>EUR</th>'
             f'<th {cellr}>INR</th></tr>' + "".join(rows) + '</table>')

    subject = (f"[Internal] {'Full' if calc['refund_type']=='full' else 'Partial'} refund — "
               f"#E-{order_id} — €{calc['refund_eur']:.2f} / ₹{calc['refund_inr']:.2f}")
    razor = calc.get("razorpay_payment_id") or "—"
    body_html = f"""
<div style="font-family: Arial, sans-serif; max-width: 640px; margin: 0 auto; padding: 20px;">
    <h2 style="margin:0 0 4px;">Refund to book — Order #E-{order_id}</h2>
    <p style="color:#64748b;font-size:13px;margin:0 0 14px;">Internal notification · EU.LensBazaar</p>
    <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:8px;">
        <tr><td {cell}>Customer</td><td {cell}>{customer}</td></tr>
        <tr><td {cell}>Email</td><td {cell}>{order.get('email','')}</td></tr>
        <tr><td {cell}>Phone</td><td {cell}>{order.get('telephone','')}</td></tr>
        <tr><td {cell}>Refund type</td><td {cell}>{'Full' if calc['refund_type']=='full' else 'Partial'}</td></tr>
        <tr><td {cell}>Reason</td><td {cell}>{reason or '—'}</td></tr>
        <tr><td {cell}>Issued by</td><td {cell}>{refunded_by or '—'}</td></tr>
        <tr><td {cell}>Razorpay payment</td><td {cell}>{razor}</td></tr>
    </table>
    <h3 style="margin-top:18px;font-size:15px;">Conversion breakdown</h3>
    {table}
    <div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:12px;margin-top:8px;font-size:14px;">
        <strong>Push to Razorpay (INR): &#8377;{calc['refund_inr']:.2f}</strong><br>
        Shown to customer (EUR): &euro;{calc['refund_eur']:.2f}
    </div>
    <hr style="margin-top:20px; border:none; border-top:1px solid #e2e8f0;">
    <p style="font-size:12px;color:#94a3b8;">EU.LensBazaar · internal refund booking notice.</p>
</div>
"""
    body_text = (f"[Internal] {'Full' if calc['refund_type']=='full' else 'Partial'} refund for "
                 f"#E-{order_id}. Customer {customer}. Reason: {reason or '-'}. "
                 f"Paid EUR {calc['paid_eur']:.2f} / INR {calc['paid_inr']:.2f}. "
                 f"Deductions INR {calc['deduction_return_inr']:.2f} (return) + "
                 f"{calc['deduction_freight_inr']:.2f} (freight). "
                 f"Push to Razorpay INR {calc['refund_inr']:.2f}. Customer EUR {calc['refund_eur']:.2f}.")
    return {"subject": subject, "body_html": body_html, "body_text": body_text}


def build_refund_email_with_ai(order: dict, calc: dict, reason: str = "") -> dict:
    """DeepSeek-drafted branded refund email. The narrative comes from the AI; the
    exact money breakdown table is appended by us (the AI is told not to invent
    numbers). Falls back to a static branded body if the AI call fails."""
    import requests as http_requests
    import config

    customer_name = f"{order.get('firstname','')} {order.get('lastname','')}".strip()
    order_id = order["order_id"]
    rt = "full refund" if calc["refund_type"] == "full" else "partial refund"
    subject = (f"Refund processed for your order #E-{order_id}"
               if calc["refund_type"] == "full"
               else f"Partial refund processed for your order #E-{order_id}")

    body_inner = None
    try:
        api_key = getattr(config, "DEEPSEEK_API_KEY", "")
        if not api_key:
            raise ValueError("DeepSeek API key not configured")
        system_prompt = (
            "You write concise, warm, professional refund notification emails for "
            "EU.LensBazaar, an online contact lens retailer. Rules:\n"
            "- Address the customer by first name\n"
            f"- Reference the order as #E-{order_id}\n"
            f"- State the refund amount as EUR {calc['refund_eur']:.2f}\n"
            "- Say the refund goes back to the original payment method within 5-7 business days "
            "and ask them to monitor their bank statement\n"
            "- If a partial refund, briefly and politely note that return-processing and "
            "freight/other charges were deducted (a detailed breakdown table is shown below your text)\n"
            "- DO NOT invent any numbers or build your own breakdown table — a breakdown table is "
            "appended separately after your text\n"
            '- Sign off "LensBazaar Customer Support"\n'
            "- Output ONLY the HTML email body with simple inline CSS, no subject line"
        )
        user_prompt = (
            f"Customer first name: {customer_name.split(' ')[0] if customer_name else 'Customer'}\n"
            f"Order: E-{order_id}\nRefund type: {rt}\n"
            f"Refund amount: EUR {calc['refund_eur']:.2f}\n"
            f"Reason: {reason or 'Refund processed by our team'}\n"
            "Write the refund notification email body (HTML)."
        )
        response = http_requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat",
                  "messages": [{"role": "system", "content": system_prompt},
                               {"role": "user", "content": user_prompt}],
                  "temperature": 0.6, "max_tokens": 900},
            timeout=30)
        response.raise_for_status()
        body_inner = response.json()["choices"][0]["message"]["content"].strip()
        if body_inner.upper().startswith("SUBJECT:"):
            parts = body_inner.split("\n", 1)
            body_inner = parts[1].strip() if len(parts) > 1 else body_inner
    except Exception as e:
        log.warning("DeepSeek refund draft failed for %s: %s", order_id, e)
        body_inner = (
            f"<p>Dear {customer_name},</p>"
            f"<p>A {rt} of <strong>&euro;{calc['refund_eur']:.2f}</strong> has been processed for "
            f"your order <strong>#E-{order_id}</strong> and returned to your original payment method. "
            "Please allow 5-7 business days for it to reflect with your bank.</p>"
            + (f"<p>Reason: {reason}</p>" if reason else "")
            + "<p>Best regards,<br><strong>LensBazaar Customer Support</strong></p>"
        )

    full_html = wrap_refund_email_body(calc, body_inner)
    text = (f"Dear {customer_name}, a {rt} of EUR {calc['refund_eur']:.2f} has been processed for "
            f"your order #E-{order_id}. Please allow 5-7 business days for it to reflect with your "
            "bank. — LensBazaar Customer Support")
    return {"subject": subject, "body_html": full_html,
            "body_text": text, "body_inner": body_inner}


def wrap_refund_email_body(calc: dict, body_inner: str) -> str:
    """Wrap an (AI-drafted or operator-edited) narrative in the EU.LensBazaar brand
    shell and ALWAYS append the code-generated EUR breakdown table, so operator edits
    to the wording can never change the refund figures."""
    return f"""
<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <div style="margin-bottom:20px;">
        <img src="https://eu.lensbazaar.com/favicon-96x96.png" alt="LensBazaar" style="width:48px; height:48px; vertical-align:middle; margin-right:10px;">
        <span style="color: #2563eb; font-size:22px; font-weight:700; vertical-align:middle;">EU.LensBazaar</span>
    </div>
    <div style="line-height:1.6; color:#333;">
        {body_inner}
    </div>
    <h3 style="margin-top:24px;color:#0f172a;font-size:15px;">Refund breakdown</h3>
    {_refund_breakdown_html(calc)}
    <hr style="margin-top:24px; border:none; border-top:1px solid #e2e8f0;">
    <p style="font-size:12px;color:#94a3b8;">EU.LensBazaar · transactional refund notification.</p>
</div>
"""


def render_refund_email_from_body(order: dict, calc: dict, body_inner: str,
                                  reason: str = "") -> dict:
    """Build the customer refund email from an operator-supplied narrative
    (edited AI draft). Subject + breakdown table are still generated by us."""
    customer_name = f"{order.get('firstname','')} {order.get('lastname','')}".strip()
    order_id = order["order_id"]
    rt = "full refund" if calc["refund_type"] == "full" else "partial refund"
    subject = (f"Refund processed for your order #E-{order_id}"
               if calc["refund_type"] == "full"
               else f"Partial refund processed for your order #E-{order_id}")
    full_html = wrap_refund_email_body(calc, body_inner or "")
    text = (f"Dear {customer_name}, a {rt} of EUR {calc['refund_eur']:.2f} has been processed for "
            f"your order #E-{order_id}. Please allow 5-7 business days for it to reflect with your "
            "bank. — LensBazaar Customer Support")
    return {"subject": subject, "body_html": full_html,
            "body_text": text, "body_inner": body_inner}




def calculate_rx_changes(order_id: int, rx_updates: dict) -> list:
    """Calculate prescription changes.

    rx_updates: {order_option_id: new_value} for prescription options (Power, Cylinder, Axis).
    Returns list of change dicts.
    """
    changes = []
    with db_cursor() as cur:
        for opt_id_str, new_val in rx_updates.items():
            opt_id = int(opt_id_str)
            cur.execute("""
                SELECT oo.order_option_id, oo.name, oo.value, oo.order_product_id,
                       op.name as product_name
                FROM order_option oo
                JOIN order_product op ON op.order_product_id = oo.order_product_id
                WHERE oo.order_option_id = %s
            """, (opt_id,))
            row = cur.fetchone()
            if not row:
                continue
            old_val = row["value"].strip()
            new_val_clean = new_val.strip()
            if old_val != new_val_clean:
                name_upper = row["name"].upper()
                if "LEFT" in name_upper or name_upper.startswith("L"):
                    eye = "LEFT"
                elif "RIGHT" in name_upper or name_upper.startswith("R"):
                    eye = "RIGHT"
                else:
                    eye = "N/A"
                changes.append({
                    "order_option_id": opt_id,
                    "order_product_id": row["order_product_id"],
                    "product_name": row["product_name"],
                    "field_name": row["name"],
                    "eye": eye,
                    "old_value": old_val,
                    "new_value": new_val_clean,
                })
    return changes


def save_rx_changes(rx_changes: list):
    """Apply prescription changes to order_option table."""
    with db_cursor() as cur:
        for c in rx_changes:
            cur.execute("""
                UPDATE order_option SET value = %s
                WHERE order_option_id = %s
            """, (c["new_value"], c["order_option_id"]))


def build_rx_email_with_ai(order: dict, rx_changes: list, qty_changes: list,
                           refund_eur: float, ticket_ref: str = "") -> dict:
    """Use DeepSeek AI to draft a smart response email for prescription/qty changes."""
    import requests as http_requests
    import config

    customer_name = f"{order.get('firstname', '')} {order.get('lastname', '')}".strip()
    order_id = order["order_id"]

    # Build a lookup of rx changes by option_id for merging
    rx_change_map = {}
    for c in rx_changes:
        rx_change_map[c["order_option_id"]] = c

    # Build FULL prescription text for each product/eye (showing all fields, marking changed ones)
    full_rx_text = "FULL UPDATED PRESCRIPTION (include ALL fields in the email, mark changed ones):\n"
    for p in order.get("products", []):
        for eye_label, eye_key in [("LEFT EYE", "left_eye"), ("RIGHT EYE", "right_eye")]:
            eye_opts = p.get(eye_key, [])
            if not eye_opts:
                continue
            full_rx_text += f"\n  {p['name']} - {eye_label}:\n"
            for opt in eye_opts:
                opt_name = opt["name"].strip()
                opt_id = opt.get("order_option_id")
                current_val = opt["value"]
                if opt_id in rx_change_map:
                    c = rx_change_map[opt_id]
                    full_rx_text += f"    {opt_name}: {c['new_value']} (CHANGED from {c['old_value']})\n"
                else:
                    full_rx_text += f"    {opt_name}: {current_val} (unchanged)\n"

    changes_text = ""
    if rx_changes:
        changes_text += "FIELDS THAT WERE CHANGED:\n"
        for c in rx_changes:
            changes_text += f"  - {c['product_name']} ({c['eye']} eye) - {c['field_name']}: {c['old_value']} -> {c['new_value']}\n"

    if qty_changes:
        changes_text += "QUANTITY CHANGES:\n"
        for c in qty_changes:
            changes_text += f"  - {c['product_name']} ({c['eye']}) - Qty: {c['old_qty']} -> {c['new_qty']} (removed {c['qty_removed']})\n"
        changes_text += f"Total Refund: EUR {refund_eur:.2f}\n"

    products_text = ""
    for p in order.get("products", []):
        products_text += f"- {p['name']} (Model: {p.get('model','')}, Qty: {p.get('quantity','')})\n"

    ticket_text = f"Ticket Reference: {ticket_ref}" if ticket_ref else "No ticket reference provided"

    system_prompt = (
        "You are a professional customer support email writer for EU.LensBazaar, an online contact lens retailer.\n\n"
        "Write clear, empathetic, professional emails. Keep them concise but warm.\n"
        "- Always address the customer by their first name\n"
        f"- Reference the order number as #E-{order_id}\n"
        "- If a ticket reference is provided, mention it in the email\n"
        "- IMPORTANT: Show the FULL UPDATED PRESCRIPTION for each eye in a table - include ALL fields (Power, Cylinder, Axis, Quantity), not just the changed ones\n"
        "- Highlight or mark the changed fields so customer can see what was updated\n"
        "- If there is a refund, mention the amount and that it will be credited within 5-7 business days\n"
        '- Sign off as "LensBazaar Customer Support"\n'
        "- Write in HTML format with simple inline CSS\n"
        "- Use a clean HTML table to show the full prescription per eye\n"
        "- DO NOT include subject line in the body"
    )

    user_prompt = (
        f"Draft a customer email for the following order changes:\n\n"
        f"ORDER DETAILS:\n"
        f"- Order ID: E-{order_id}\n"
        f"- Customer Name: {customer_name}\n"
        f"- {ticket_text}\n"
        f"- Products:\n{products_text}\n"
        f"{full_rx_text}\n"
        f"{changes_text}\n"
        f"Please provide:\n"
        f'1. A suitable email subject line (on its own line, prefixed with "SUBJECT: ")\n'
        f"2. The email body in HTML format (after a blank line)\n\n"
        f"The email MUST include the COMPLETE updated prescription for each eye in a table "
        f"(Power, Cylinder, Axis, Quantity - all fields), highlighting which fields were changed. "
        f"The customer needs to verify the FULL prescription is correct, not just the changed fields."
    )

    try:
        api_key = getattr(config, "DEEPSEEK_API_KEY", "")
        if not api_key:
            raise ValueError("DeepSeek API key not configured")

        response = http_requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 2000
            },
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        ai_content = data["choices"][0]["message"]["content"].strip()

        subject = f"Order #E-{order_id} - Prescription Update"
        body_html = ai_content

        if "SUBJECT:" in ai_content:
            for i, line in enumerate(ai_content.split("\n")):
                if line.strip().startswith("SUBJECT:"):
                    subject = line.strip().replace("SUBJECT:", "").strip()
                    remaining = "\n".join(ai_content.split("\n")[i+1:]).strip()
                    if remaining:
                        body_html = remaining
                    break

        full_html = f"""
<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <div style="margin-bottom:20px;">
        <img src="https://eu.lensbazaar.com/favicon-96x96.png" alt="LensBazaar" style="width:48px; height:48px; vertical-align:middle; margin-right:10px;">
        <span style="color: #2563eb; font-size:22px; font-weight:700; vertical-align:middle;">EU.LensBazaar</span>
    </div>
    <div style="line-height:1.6; color:#333;">
        {body_html}
    </div>
    <hr style="margin-top:30px; border:none; border-top:1px solid #e2e8f0;">
    <p style="font-size:12px; color:#64748b; margin-top:12px;">
        For any queries, please reply to this email or use our
        <a href="https://eu.lensbazaar.com/index.php?route=information/contact" style="color:#2563eb;">contact form</a>.
    </p>
</div>"""

        return {
            "subject": subject,
            "body_html": full_html,
            "to": order.get("email", ""),
            "bcc": "admin@lensbazaar.com",
        }

    except Exception as e:
        log.error("DeepSeek API error for RX email: %s", e)
        return _build_rx_email_fallback(order, rx_changes, qty_changes, refund_eur, ticket_ref)

def _build_rx_email_fallback(order, rx_changes, qty_changes, refund_eur, ticket_ref):
    """Fallback email template if DeepSeek fails."""
    customer_name = f"{order.get('firstname', '')} {order.get('lastname', '')}".strip()
    order_id = order["order_id"]

    rx_rows = ""
    for c in rx_changes:
        rx_rows += f"""<tr>
            <td style="padding:8px; border:1px solid #e2e8f0;">{c['product_name']}</td>
            <td style="padding:8px; border:1px solid #e2e8f0;">{c['eye']}</td>
            <td style="padding:8px; border:1px solid #e2e8f0;">{c['field_name']}</td>
            <td style="padding:8px; border:1px solid #e2e8f0; color:#dc2626;">{c['old_value']}</td>
            <td style="padding:8px; border:1px solid #e2e8f0; color:#16a34a; font-weight:600;">{c['new_value']}</td>
        </tr>"""

    ticket_line = f"<p>Reference: <strong>{ticket_ref}</strong></p>" if ticket_ref else ""
    refund_block = ""
    if refund_eur > 0:
        refund_block = f"""<div style="background:#f0fdf4; border:1px solid #86efac; border-radius:8px; padding:16px; margin:20px 0;">
            <p style="margin:0;">Refund Amount: <strong>&euro;{refund_eur:.2f}</strong> - will be credited within 5-7 business days.</p>
        </div>"""

    rx_table = ""
    if rx_rows:
        rx_table = f"""<h3>Prescription Changes</h3>
        <table style="width:100%; border-collapse:collapse;">
            <thead><tr style="background:#f1f5f9;">
                <th style="padding:8px; border:1px solid #e2e8f0;">Product</th>
                <th style="padding:8px; border:1px solid #e2e8f0;">Eye</th>
                <th style="padding:8px; border:1px solid #e2e8f0;">Field</th>
                <th style="padding:8px; border:1px solid #e2e8f0;">Previous</th>
                <th style="padding:8px; border:1px solid #e2e8f0;">Updated</th>
            </tr></thead>
            <tbody>{rx_rows}</tbody>
        </table>"""

    body = f"""
<div style="font-family:Arial,sans-serif; max-width:600px; margin:0 auto; padding:20px;">
    <div style="margin-bottom:20px;">
        <img src="https://eu.lensbazaar.com/favicon-96x96.png" alt="LensBazaar" style="width:48px; height:48px; vertical-align:middle; margin-right:10px;">
        <span style="color:#2563eb; font-size:22px; font-weight:700; vertical-align:middle;">EU.LensBazaar</span>
    </div>
    <p>Dear {customer_name},</p>
    <p>Your order <strong>#E-{order_id}</strong> has been updated as requested.</p>
    {ticket_line}
    {rx_table}
    {refund_block}
    <p>Please verify the updated details. If anything looks incorrect, reply to this email.</p>
    <p>Best regards,<br><strong>LensBazaar Customer Support</strong></p>
</div>"""

    return {
        "subject": f"Order #E-{order_id} - Prescription Updated",
        "body_html": body,
        "to": order.get("email", ""),
        "bcc": "admin@lensbazaar.com",
    }



def build_customer_email(order: dict, changes: list, refund_eur: float,
                         eur_rate: float = 0) -> dict:
    """Build draft email HTML for the customer about order update.

    Shows ALL products with updated quantities. Changed items show (previous was X).
    Includes product summary table with EUR prices at bottom.
    """
    customer_name = f"{order.get('firstname', '')} {order.get('lastname', '')}".strip()
    order_id = order["order_id"]
    if eur_rate <= 0:
        eur_rate = float(order.get("currency_value") or 0.0108)

    # Build a lookup of changed options: {order_option_id: {old_qty, new_qty}}
    changed_opts = {}
    for c in changes:
        changed_opts[c["order_option_id"]] = {
            "old_qty": c["old_qty"],
            "new_qty": c["new_qty"],
        }

    # Build product list HTML showing ALL products with updated quantities
    products_html = ""
    for idx, p in enumerate(order.get("products", []), 1):
        total_qty = 0
        eye_sections = []

        for eye_label, eye_key in [("LEFT EYE", "left_eye"), ("RIGHT EYE", "right_eye")]:
            eye_opts = p.get(eye_key, [])
            if not eye_opts:
                continue
            opts_html = ""
            for opt in eye_opts:
                opt_name = opt["name"].strip()
                opt_value = opt["value"]
                opt_id = opt.get("order_option_id")

                if opt_name.lower() == "quantity":
                    if opt_id in changed_opts:
                        old_q = changed_opts[opt_id]["old_qty"]
                        new_q = changed_opts[opt_id]["new_qty"]
                        total_qty += new_q
                        opts_html += f'     {opt_name}: {new_q} (previous was {old_q})<br>'
                    else:
                        total_qty += int(opt_value)
                        opts_html += f'     {opt_name}: {opt_value}<br>'
                else:
                    opts_html += f'     {opt_name}: {opt_value}<br>'

            eye_sections.append(f'   <strong>{eye_label}:</strong><br>{opts_html}')

        if not eye_sections:
            total_qty = int(p.get("quantity", 0))

        products_html += f"""
        <div style="margin-bottom:16px; padding:12px; background:#f8fafc; border:1px solid #e2e8f0; border-radius:6px;">
            <strong>{idx}. {p['name']}</strong><br>
            Model: {p['model']}<br>
            Total Qty: {total_qty}<br>
            {'<br>'.join(eye_sections)}
        </div>"""

    # Build product summary table with EUR prices
    table_rows = ""
    for p in order.get("products", []):
        unit_price_eur = float(p["price"]) * eur_rate
        # Recalculate qty from eye options if available
        updated_qty = 0
        has_eyes = False
        for eye_key in ["left_eye", "right_eye"]:
            for opt in p.get(eye_key, []):
                if opt["name"].strip().lower() == "quantity":
                    opt_id = opt.get("order_option_id")
                    if opt_id in changed_opts:
                        updated_qty += changed_opts[opt_id]["new_qty"]
                    else:
                        updated_qty += int(opt["value"])
                    has_eyes = True
        if not has_eyes:
            updated_qty = int(p.get("quantity", 0))
        total_eur = unit_price_eur * updated_qty
        table_rows += f"""
            <tr>
                <td style="padding:8px; border:1px solid #e2e8f0;">{p['name']}</td>
                <td style="padding:8px; border:1px solid #e2e8f0; text-align:center;">{updated_qty}</td>
                <td style="padding:8px; border:1px solid #e2e8f0; text-align:right;">&euro;{unit_price_eur:.2f}</td>
                <td style="padding:8px; border:1px solid #e2e8f0; text-align:right;">&euro;{total_eur:.2f}</td>
            </tr>"""

    subject = f"Order #E-{order_id} - Your order has been updated"

    body_html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <div style="margin-bottom:20px;">
            <img src="https://eu.lensbazaar.com/favicon-96x96.png" alt="LensBazaar" style="width:48px; height:48px; vertical-align:middle; margin-right:10px;">
            <span style="color: #2563eb; font-size:22px; font-weight:700; vertical-align:middle;">EU.LensBazaar</span>
        </div>

        <p>Dear {customer_name},</p>

        <p>Thank you for your order <strong>#E-{order_id}</strong>.</p>

        <p>Your order items has been updated:</p>

        <h3 style="margin-top:20px;">Products:</h3>
        {products_html}

        <div style="background: #f0fdf4; border: 1px solid #86efac; border-radius: 8px;
                    padding: 16px; margin: 20px 0;">
            <p style="margin:0;">This has resulted in a <strong>Refund Amount of: &euro;{refund_eur:.2f}</strong></p>
            <p style="margin: 8px 0 0 0; font-size: 13px;">
                The refund will be credited to your original payment method within 5-7 business days.
            </p>
        </div>

        <p>The remaining below order is being shipped now and our team will share a FedEx AWB shortly.</p>

        <table style="width:100%; border-collapse:collapse; margin:16px 0;">
            <thead>
                <tr style="background:#f1f5f9;">
                    <th style="padding:8px; border:1px solid #e2e8f0; text-align:left;">Product Name</th>
                    <th style="padding:8px; border:1px solid #e2e8f0; text-align:center;">Qty</th>
                    <th style="padding:8px; border:1px solid #e2e8f0; text-align:right;">Unit Price (EUR)</th>
                    <th style="padding:8px; border:1px solid #e2e8f0; text-align:right;">Total (EUR)</th>
                </tr>
            </thead>
            <tbody>
                {table_rows}
            </tbody>
        </table>

        <p>For any queries, please reply back to this email or use our contact us form by
        <a href="https://eu.lensbazaar.com/index.php?route=information/contact">clicking here</a>.</p>

        <p>Best regards,<br>
        <strong>LensBazaar Customer Support</strong></p>
    </div>
    """

    return {
        "to": order.get("email", ""),
        "subject": subject,
        "body_html": body_html,
        "bcc": "admin@lensbazaar.com",
    }


def build_product_change_email_with_ai(order: dict, product_changes: list,
                                       ticket_ref: str = "", price_difference: float = 0.0) -> dict:
    """Use DeepSeek AI to draft a smart email for product change notification.
    
    product_changes: list of dicts with old_name, new_name, rx_summary, order_product_id, prices
    price_difference: positive = customer owes more, negative = refund due
    """
    import requests as http_requests
    import config

    customer_name = f"{order.get('firstname', '')} {order.get('lastname', '')}".strip()
    order_id = order["order_id"]

    # Build product changes text
    changes_text = "PRODUCT CHANGES:\n"
    for c in product_changes:
        changes_text += f"  - Changed from: {c['old_name']} -> New product: {c['new_name']}\n"
        if c.get('old_price_eur') and c.get('new_price_eur'):
            changes_text += f"    Price: EUR {c['old_price_eur']:.2f} -> EUR {c['new_price_eur']:.2f} (qty: {c.get('quantity', 1)})\n"
        if c.get('rx_summary'):
            changes_text += f"    Prescription (unchanged): {c['rx_summary']}\n"
    
    if price_difference > 0:
        changes_text += f"\nADDITIONAL AMOUNT RECEIVABLE FROM CUSTOMER: EUR {price_difference:.2f}\n"
        changes_text += "(New product costs more — customer needs to pay the difference)\n"
    elif price_difference < 0:
        changes_text += f"\nAMOUNT REFUNDABLE TO CUSTOMER: EUR {abs(price_difference):.2f}\n"
        changes_text += "(New product costs less — customer will receive a refund)\n"

    # Build full order details for context
    products_text = "CURRENT ORDER PRODUCTS (after change):\n"
    for p in order.get("products", []):
        products_text += f"- {p['name']} (Model: {p.get('model','')}, Qty: {p.get('quantity','')})\n"
        for eye_label, eye_key in [("LEFT EYE", "left_eye"), ("RIGHT EYE", "right_eye")]:
            eye_opts = p.get(eye_key, [])
            if eye_opts:
                products_text += f"    {eye_label}: "
                opts = []
                for opt in eye_opts:
                    opts.append(f"{opt['name']}: {opt['value']}")
                products_text += ", ".join(opts) + "\n"

    ticket_text = f"Ticket Reference: {ticket_ref}" if ticket_ref else "No ticket reference provided"

    system_prompt = (
        "You are a professional customer support email writer for EU.LensBazaar, an online contact lens retailer.\n\n"
        "Write clear, empathetic, professional emails. Keep them concise but warm.\n"
        "- Always address the customer by their first name\n"
        f"- Reference the order number as #E-{order_id}\n"
        "- If a ticket reference is provided, mention it in the email\n"
        "- Explain that the product has been changed as per their request\n"
        "- If there is a price difference, clearly state the amount receivable or refundable\n"
        "- For receivable: mention the additional amount the customer needs to pay\n"
        "- For refundable: mention the refund amount and that it will be credited in 5-7 business days\n"
        "- Show the NEW product details clearly including what will be shipped\n"
        "- Include the prescription data that remains the same\n"
        "- Mention that the order will be shipped shortly with the updated product\n"
        "- Ask customer to confirm if this is correct\n"
        '- Sign off as "LensBazaar Customer Support"\n'
        "- Write in HTML format with simple inline CSS\n"
        "- Use a clean layout with product details highlighted\n"
        "- DO NOT include subject line in the body"
    )

    user_prompt = (
        f"Draft a customer email for the following product change:\n\n"
        f"ORDER DETAILS:\n"
        f"- Order ID: E-{order_id}\n"
        f"- Customer Name: {customer_name}\n"
        f"- {ticket_text}\n\n"
        f"{changes_text}\n"
        f"{products_text}\n"
        f"Please provide:\n"
        f'1. A suitable email subject line (on its own line, prefixed with "SUBJECT: ")\n'
        f"2. The email body in HTML format (after a blank line)\n\n"
        f"The email should clearly communicate:\n"
        f"- What product was changed from and to\n"
        f"- The prescription remains the same\n"
        f"- The new product will be shipped now\n"
        f"- Ask customer to confirm the change is acceptable"
    )

    try:
        api_key = getattr(config, "DEEPSEEK_API_KEY", "")
        if not api_key:
            raise ValueError("DeepSeek API key not configured")

        response = http_requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 2000
            },
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        ai_content = data["choices"][0]["message"]["content"].strip()

        subject = f"Order #E-{order_id} - Product Change Update"
        body_html = ai_content

        if "SUBJECT:" in ai_content:
            for i, line in enumerate(ai_content.split("\n")):
                if line.strip().startswith("SUBJECT:"):
                    subject = line.strip().replace("SUBJECT:", "").strip()
                    remaining = "\n".join(ai_content.split("\n")[i+1:]).strip()
                    if remaining:
                        body_html = remaining
                    break

        full_html = f"""
<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <div style="margin-bottom:20px;">
        <img src="https://eu.lensbazaar.com/favicon-96x96.png" alt="LensBazaar" style="width:48px; height:48px; vertical-align:middle; margin-right:10px;">
        <span style="color: #2563eb; font-size:22px; font-weight:700; vertical-align:middle;">EU.LensBazaar</span>
    </div>
    <div style="line-height:1.6; color:#333;">
        {body_html}
    </div>
    <hr style="margin-top:30px; border:none; border-top:1px solid #e2e8f0;">
    <p style="font-size:12px; color:#64748b; margin-top:12px;">
        For any queries, please reply to this email or use our
        <a href="https://eu.lensbazaar.com/index.php?route=information/contact" style="color:#2563eb;">contact form</a>.
    </p>
</div>"""

        return {
            "subject": subject,
            "body_html": full_html,
            "to": order.get("email", ""),
            "bcc": "admin@lensbazaar.com",
        }

    except Exception as e:
        log.error("DeepSeek API error for product change email: %s", e)
        return _build_product_change_email_fallback(order, product_changes, ticket_ref)


def _build_product_change_email_fallback(order: dict, product_changes: list, ticket_ref: str = "") -> dict:
    """Fallback email template if DeepSeek fails."""
    customer_name = f"{order.get('firstname', '')} {order.get('lastname', '')}".strip()
    order_id = order["order_id"]

    changes_rows = ""
    for c in product_changes:
        changes_rows += f"""<tr>
            <td style="padding:8px; border:1px solid #e2e8f0; color:#dc2626; text-decoration:line-through;">{c['old_name']}</td>
            <td style="padding:8px; border:1px solid #e2e8f0; color:#16a34a; font-weight:600;">{c['new_name']}</td>
            <td style="padding:8px; border:1px solid #e2e8f0; font-size:12px;">{c.get('rx_summary', 'Same as before')}</td>
        </tr>"""

    ticket_line = f"<p>Reference: <strong>{ticket_ref}</strong></p>" if ticket_ref else ""

    body = f"""
<div style="font-family:Arial,sans-serif; max-width:600px; margin:0 auto; padding:20px;">
    <div style="margin-bottom:20px;">
        <img src="https://eu.lensbazaar.com/favicon-96x96.png" alt="LensBazaar" style="width:48px; height:48px; vertical-align:middle; margin-right:10px;">
        <span style="color:#2563eb; font-size:22px; font-weight:700; vertical-align:middle;">EU.LensBazaar</span>
    </div>
    <p>Dear {customer_name},</p>
    <p>Your order <strong>#E-{order_id}</strong> has been updated with a product change as requested.</p>
    {ticket_line}
    <h3>Product Change:</h3>
    <table style="width:100%; border-collapse:collapse;">
        <thead><tr style="background:#f1f5f9;">
            <th style="padding:8px; border:1px solid #e2e8f0;">Previous Product</th>
            <th style="padding:8px; border:1px solid #e2e8f0;">New Product</th>
            <th style="padding:8px; border:1px solid #e2e8f0;">Prescription</th>
        </tr></thead>
        <tbody>{changes_rows}</tbody>
    </table>
    <p style="margin-top:16px;">Your prescription remains unchanged. The updated product will be shipped shortly and our team will share a FedEx AWB.</p>
    <p>If you have any questions, please reply to this email.</p>
    <p>Best regards,<br><strong>LensBazaar Customer Support</strong></p>
</div>"""

    return {
        "subject": f"Order #E-{order_id} - Product Change Confirmation",
        "body_html": body,
        "to": order.get("email", ""),
        "bcc": "admin@lensbazaar.com",
    }


def save_product_change(order_id: int, product_changes: list, edited_by: int, notes: str = ""):
    """Save product name change to order_product table and log it."""
    with db_cursor() as cur:
        for c in product_changes:
            new_pid = str(c.get("new_product_id") or "").strip()
            new_name = c["new_name"]
            new_model = c["new_name"]
            # If a catalog product was selected, pull its canonical name + model
            # so order_product.name / model / product_id all reflect the new item.
            if new_pid:
                cur.execute("""
                    SELECT p.product_id, p.model, pd.name
                    FROM product p
                    LEFT JOIN product_description pd ON pd.product_id = p.product_id
                    WHERE p.product_id = %s LIMIT 1
                """, (new_pid,))
                prow = cur.fetchone()
                if prow:
                    new_model = prow["model"] or new_model
                    if prow.get("name"):
                        new_name = prow["name"]
                    cur.execute("""
                        UPDATE order_product SET name = %s, model = %s, product_id = %s
                        WHERE order_product_id = %s
                    """, (new_name, new_model, prow["product_id"], c["order_product_id"]))
                    continue
            # Fallback (no catalog product id): keep prior behaviour, and also
            # sync model to the entered value so the two columns stay consistent.
            cur.execute("""
                UPDATE order_product SET name = %s, model = %s
                WHERE order_product_id = %s
            """, (new_name, new_model, c["order_product_id"]))
        
        # Log the edit
        cur.execute("""
            INSERT INTO order_edits
                (order_id, edit_data, refund_amount_eur, refund_amount_inr,
                 razorpay_payment_id, refund_status, edited_by, notes, created_at)
            VALUES (%s, %s, 0, 0, '', 'none', %s, %s, NOW())
        """, (order_id, json.dumps({"type": "product_change", "changes": product_changes}),
              edited_by, notes))
        return cur.lastrowid
