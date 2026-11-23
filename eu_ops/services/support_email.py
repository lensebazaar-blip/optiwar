"""Support Email Service — AI-powered email drafting via DeepSeek for customer support."""
import logging
import requests
import config

logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_API_KEY = getattr(config, "DEEPSEEK_API_KEY", "")


def _followup_context(previous) -> str:
    """Prompt block describing the earlier email a follow-up continues."""
    if not previous:
        return ""
    body = (previous.get('text') or '')[:4000]
    if not body:
        body = ("(The text of that email was not kept on record; only its subject and "
                "date are known. Refer to it as 'our earlier email' on that date.)")
    return f"""

THIS IS A FOLLOW-UP. We already sent the customer this email on {previous.get('sent_at','')}:
--- PREVIOUS SUBJECT: {previous.get('subject','')}
--- PREVIOUS BODY (plain text):
{body}
--- END OF PREVIOUS EMAIL
Write the new email as a continuation: refer to the earlier message briefly, do not repeat
it, and keep the same subject with a "Re: " prefix unless the agent's notes say otherwise."""


def draft_support_email(order: dict, agent_notes: str, previous: dict = None) -> dict:
    """Use DeepSeek to draft a professional customer email based on agent notes.
    
    Args:
        order: Full order dict with customer info, products, etc.
        agent_notes: Free-text description from agent about the issue.
    
    Returns:
        dict with 'subject' and 'body_html' keys.
    """
    customer_name = f"{order.get('firstname', '')} {order.get('lastname', '')}".strip()
    order_id = order.get("order_id", "")
    customer_email = order.get("email", "")
    
    # Build product summary for context
    products_text = ""
    for p in order.get("products", []):
        products_text += f"- {p['name']} (Model: {p.get('model','')}, Qty: {p.get('quantity','')})\n"
    
    shipping_address = (
        f"{order.get('shipping_firstname','')} {order.get('shipping_lastname','')}, "
        f"{order.get('shipping_address_1','')}, {order.get('shipping_city','')}, "
        f"{order.get('shipping_postcode','')}, {order.get('shipping_country','')}"
    )

    system_prompt = """You are a professional customer support email writer for EU.LensBazaar, an online contact lens retailer based in Europe. 

Write clear, empathetic, professional emails. Keep them concise but warm. 
- Always address the customer by their first name
- Reference the order number as #E-{order_id}
- Sign off as "LensBazaar Customer Support"
- Include a line about contacting us for further queries
- DO NOT include subject line in the body
- Write in HTML format with simple styling (no complex CSS, just basic paragraphs and line breaks)
- Use <br> for line breaks within paragraphs and <p> for paragraph separation
- Keep the tone professional yet friendly"""

    user_prompt = f"""Draft a customer support email for the following situation:

ORDER DETAILS:
- Order ID: E-{order_id}
- Customer Name: {customer_name}
- Customer Email: {customer_email}
- Products: 
{products_text}
- Shipping Address: {shipping_address}
- Order Status: {order.get('status_name', 'Unknown')}

AGENT'S NOTES (what happened / what to communicate):
{agent_notes}
{_followup_context(previous)}

Please provide:
1. A suitable email subject line (on its own line, prefixed with "SUBJECT: ")
2. The email body in HTML format (after a blank line)

The email should clearly communicate what the agent described, be professional, and provide any relevant next steps for the customer."""

    try:
        api_key = DEEPSEEK_API_KEY or getattr(config, "DEEPSEEK_API_KEY", "")
        if not api_key:
            raise ValueError("DeepSeek API key not configured")
        
        response = requests.post(
            DEEPSEEK_API_URL,
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
        content = data["choices"][0]["message"]["content"].strip()
        
        # Parse subject and body
        subject = f"Order #E-{order_id} - Support Update"
        body_html = content
        
        if "SUBJECT:" in content:
            parts = content.split("\n", 1)
            for i, line in enumerate(content.split("\n")):
                if line.strip().startswith("SUBJECT:"):
                    subject = line.strip().replace("SUBJECT:", "").strip()
                    # Body is everything after the subject line (skip blank lines)
                    remaining = "\n".join(content.split("\n")[i+1:]).strip()
                    if remaining:
                        body_html = remaining
                    break
        
        # Wrap in a nice email template
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
            "to": customer_email,
            "bcc": "admin@lensbazaar.com"
        }
        
    except Exception as e:
        logger.error("DeepSeek API error: %s", e)
        raise ValueError(f"Failed to generate email draft: {str(e)}")


def draft_ow_support_email(order: dict, agent_notes: str, previous: dict = None) -> dict:
    """DeepSeek-drafted customer support email for OPTIWAR orders.
    Sends from admin@optiwar.com, Optiwar branding, order id OW-{id}."""
    customer_name = f"{order.get('firstname', '')} {order.get('lastname', '')}".strip()
    order_id = order.get("order_id", "")
    customer_email = order.get("email", "")

    products_text = ""
    for p in order.get("products", []):
        rx = p.get("rx") or {}
        rec = rx.get("recommendations") or ""
        products_text += f"- {p.get('name','')} (Code: {p.get('model','')}, Qty: {p.get('quantity','')})"
        products_text += f" [Lens: {rec}]\n" if rec else "\n"

    shipping_address = (
        f"{order.get('shipping_address_1','')}, {order.get('shipping_zone','')}, "
        f"{order.get('shipping_postcode','')}, {order.get('shipping_country','')}"
    )

    system_prompt = """You are a professional customer support email writer for Optiwar, an online premium eyewear retailer.

Write clear, empathetic, professional emails. Keep them concise but warm.
- Always address the customer by their first name
- Reference the order number as OW-{order_id}
- Sign off as "Optiwar Customer Support"
- Include a line about contacting us for further queries
- DO NOT include subject line in the body
- Write in HTML format with simple styling (basic paragraphs and line breaks only)
- Use <br> for line breaks within paragraphs and <p> for paragraph separation
- Keep the tone professional yet friendly"""

    user_prompt = f"""Draft a customer support email for the following situation:

ORDER DETAILS:
- Order ID: OW-{order_id}
- Customer Name: {customer_name}
- Customer Email: {customer_email}
- Products:
{products_text}
- Shipping Address: {shipping_address}
- Order Status: {order.get('status_name', 'Unknown')}

AGENT'S NOTES (what happened / what to communicate):
{agent_notes}
{_followup_context(previous)}

Please provide:
1. A suitable email subject line (on its own line, prefixed with "SUBJECT: ")
2. The email body in HTML format (after a blank line)

The email should clearly communicate what the agent described, be professional, and provide any relevant next steps for the customer."""

    api_key = DEEPSEEK_API_KEY or getattr(config, "DEEPSEEK_API_KEY", "")
    if not api_key:
        raise ValueError("DeepSeek API key not configured")

    response = requests.post(
        DEEPSEEK_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": "deepseek-chat",
              "messages": [{"role": "system", "content": system_prompt},
                           {"role": "user", "content": user_prompt}],
              "temperature": 0.7, "max_tokens": 2000},
        timeout=30)
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"].strip()

    subject = f"Order OW-{order_id} - Support Update"
    body_html = content
    if "SUBJECT:" in content:
        for i, line in enumerate(content.split("\n")):
            if line.strip().startswith("SUBJECT:"):
                subject = line.strip().replace("SUBJECT:", "").strip()
                remaining = "\n".join(content.split("\n")[i+1:]).strip()
                if remaining:
                    body_html = remaining
                break

    logo = f"{getattr(config, 'PUBLIC_BASE_URL', 'https://eu.lensbazaar.com')}/ops/static/img/optiwar-logo.png"
    full_html = f"""
<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <div style="margin-bottom:20px;">
        <img src="{logo}" alt="Optiwar" style="height:44px; vertical-align:middle; border-radius:8px;">
    </div>
    <div style="line-height:1.6; color:#333;">
        {body_html}
    </div>
    <hr style="margin-top:30px; border:none; border-top:1px solid #e2e8f0;">
    <p style="font-size:12px; color:#64748b; margin-top:12px;">
        For any queries, please reply to this email or write to
        <a href="mailto:admin@optiwar.com" style="color:#e67e22;">admin@optiwar.com</a>.
    </p>
</div>"""

    return {
        "subject": subject,
        "body_html": full_html,
        "to": customer_email,
        "bcc": config.OPTIWAR_ADMIN_CC,
    }
