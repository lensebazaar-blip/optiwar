"""Notification service — Email, WhatsApp (Meta), SMS (Twilio).

WhatsApp implementation mirrors the PHP code in
/var/www/lbws/lensbazaar.asia/admin/model/sale/order.php
"""
import json
import re
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from base64 import b64encode
from html import escape as html_escape

import requests

import config
from services import ow_rx

log = logging.getLogger("notifications")

# WhatsApp API version
WA_API_VERSION = "v18.0"

# Template → language and param count (matches approved Meta templates)
WA_TEMPLATES = {
    "orders_shipped_eu":  {"lang": "en",    "params": 2},  # {{1}}=order_id, {{2}}=awb (EU/FedEx)
    "orders_shipped":     {"lang": "en_US", "params": 2},  # {{1}}=order_id, {{2}}=awb (India)
    "order_cancelled":    {"lang": "en",    "params": 1},  # {{1}}=order_id
    "order_refunded":     {"lang": "en",    "params": 1},  # {{1}}=order_id
    "order_cod_verified": {"lang": "en",    "params": 1},  # {{1}}=order_id
    "order_notify":       {"lang": "en_US", "params": 1},  # {{1}}=order_id
    "order_pending":      {"lang": "en",    "params": 1},  # {{1}}=order_id
    "order_delay":        {"lang": "en_US", "params": 1},  # {{1}}=date text
    # {{1}}=name, {{2}}=order id, {{3}}=items, {{4}}=amount, {{5}}=site host
    "ow_order_created":   {"lang": "en",    "params": 5},
}

# Order status_id → WhatsApp template name
# EU platform uses orders_shipped_eu for FedEx shipments
STATUS_TEMPLATE_MAP = {
    3:  "orders_shipped_eu",   # Shipped — EU/FedEx with AWB
    7:  "order_cancelled",     # Canceled
    11: "order_refunded",      # Refunded
    17: "order_cod_verified",  # confirm on COD
}


# ─── Email ───────────────────────────────────────────────────────────

def _split_addrs(value) -> list:
    return [addr.strip() for addr in str(value or "").split(",") if addr.strip()]


def send_email(to: str, subject: str, body_html: str, body_text: str = None,
               cc: str = None, from_addr: str = None, admin_cc: str = "admin@lensbazaar.com",
               smtp_host: str = None, smtp_port: int = None,
               smtp_user: str = None, smtp_pass: str = None):
    host = smtp_host or config.SMTP_HOST
    port = smtp_port or config.SMTP_PORT
    user = smtp_user or config.SMTP_USER
    pwd = smtp_pass or config.SMTP_PASS
    sender = from_addr or config.SMTP_FROM or config.SMTP_USER

    if not all([host, user, pwd]):
        log.warning("SMTP not configured — skipping email to %s", to)
        return False, "SMTP not configured"

    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = to
    try:
        domain = sender.split("@")[-1] if "@" in sender else "lensbazaar.com"
        msg_id = make_msgid(domain=domain)
    except Exception:
        msg_id = make_msgid()
    msg["Message-ID"] = msg_id
    cc_list = []
    seen_cc = set()
    to_addr = (to or "").strip().casefold()
    for addr in _split_addrs(cc) + _split_addrs(admin_cc):
        normalized = addr.casefold()
        if normalized == to_addr or normalized in seen_cc:
            continue
        seen_cc.add(normalized)
        cc_list.append(addr)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject

    if body_text:
        msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    recipients = [to] + cc_list
    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(user, pwd)
            server.sendmail(sender, recipients, msg.as_string())
        log.info("Email sent to %s (from: %s, cc: %s) — subject: %s", to, sender, cc_list, subject)
        return True, f"Email sent (id: {msg_id})"
    except Exception as e:
        log.error("Email FAILED to %s: %s", to, e)
        return False, str(e)


def send_otp_email(to: str, otp_code: str):
    subject = f"LensBazaar OPP - Your login code: {otp_code}"
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #2563eb;">LensBazaar Order Processing</h2>
        <p>Your one-time verification code is:</p>
        <div style="font-size: 32px; font-weight: bold; letter-spacing: 8px; text-align: center;
                    padding: 20px; background: #f1f5f9; border-radius: 8px; margin: 20px 0;">
            {otp_code}
        </div>
        <p>This code expires in {config.OTP_EXPIRE_MINUTES} minutes.</p>
        <p style="color: #64748b; font-size: 12px;">If you didn't request this code, ignore this email.</p>
    </div>
    """
    return send_email(to, subject, html, f"Your OTP code: {otp_code}")


# ─── WhatsApp (Meta Cloud API) ───────────────────────────────────────

# ISO 3166-1 alpha-2 → E.164 calling code (EU + common countries)
COUNTRY_CALLING_CODES = {
    "AT": "43", "BE": "32", "BG": "359", "HR": "385", "CY": "357",
    "CZ": "420", "DK": "45", "EE": "372", "FI": "358", "FR": "33",
    "DE": "49", "GR": "30", "HU": "36", "IE": "353", "IT": "39",
    "LV": "371", "LT": "370", "LU": "352", "MT": "356", "NL": "31",
    "PL": "48", "PT": "351", "RO": "40", "SK": "421", "SI": "386",
    "ES": "34", "SE": "46", "GB": "44", "CH": "41", "NO": "47",
    "IN": "91", "US": "1", "CA": "1", "AU": "61", "AE": "971",
    "SA": "966", "SG": "65", "MY": "60", "PH": "63", "JP": "81",
}


def _format_wa_phone(phone: str, country_code: str = "") -> str:
    """Normalize phone to E.164 international format (digits only, no +).
    Uses country_code (ISO 3166-1 alpha-2, e.g. 'AT') to prepend calling code
    when the number appears to be in local/domestic format."""
    phone = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if phone.startswith("+"):
        phone = phone[1:]
    if phone.startswith("00"):
        phone = phone[2:]

    cc_upper = country_code.strip().upper() if country_code else ""
    dial_code = COUNTRY_CALLING_CODES.get(cc_upper, "")

    if dial_code:
        # Country code is known — check if number already starts with it
        if phone.startswith(dial_code):
            return phone  # already in international format
        # Strip leading 0 (domestic trunk prefix common in EU)
        if phone.startswith("0"):
            phone = phone[1:]
        phone = dial_code + phone
    else:
        # No country code provided — try heuristic
        if phone.startswith("0"):
            pass  # can't determine country, leave as-is
        elif len(phone) == 10 and phone[0] in "6789":
            phone = "91" + phone  # likely Indian

    return phone


def _wa_send(phone: str, payload: dict):
    """Low-level WhatsApp send via Meta Graph API.
    Returns (success, message). Checks response for message ID like PHP does."""
    if not all([config.WA_SYSTEM_TOKEN, config.WA_PHONE_NUMBER_ID]):
        log.warning("WhatsApp not configured (missing token or phone_number_id)")
        return False, "WhatsApp not configured"

    url = f"https://graph.facebook.com/{WA_API_VERSION}/{config.WA_PHONE_NUMBER_ID}/messages"
    data = json.dumps(payload).encode()
    req = Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {config.WA_SYSTEM_TOKEN}")
    req.add_header("Content-Type", "application/json")

    log.info("WhatsApp sending to %s — payload: %s", phone, json.dumps(payload)[:200])

    try:
        with urlopen(req, timeout=30) as resp:
            resp_body = resp.read().decode()
            result = json.loads(resp_body)

        # Check for message ID in response (same pattern as PHP)
        contacts = result.get("contacts", [])
        messages = result.get("messages", [])

        if messages and messages[0].get("id"):
            msg_id = messages[0]["id"]
            to_num = contacts[0]["input"] if contacts else phone
            log.info("WhatsApp SENT to +%s — id: %s", to_num, msg_id)
            return True, f"WhatsApp sent (id: {msg_id})"
        else:
            log.error("WhatsApp NO MSG ID — response: %s", resp_body[:500])
            return False, "No message ID in WhatsApp response"

    except HTTPError as e:
        err_body = e.read().decode()
        log.error("WhatsApp HTTP %d to %s: %s", e.code, phone, err_body[:500])
        # Extract Meta error message if available
        try:
            err_data = json.loads(err_body)
            meta_msg = err_data.get("error", {}).get("message", str(e))
            return False, f"WhatsApp API error {e.code}: {meta_msg}"
        except Exception:
            return False, f"WhatsApp HTTP {e.code}: {err_body[:200]}"

    except URLError as e:
        log.error("WhatsApp network error to %s: %s", phone, e)
        return False, f"WhatsApp network error: {e}"

    except Exception as e:
        log.error("WhatsApp unexpected error to %s: %s", phone, e)
        return False, f"WhatsApp error: {e}"


def send_wa_template(phone: str, template_name: str, language: str = "en",
                     body_params: list = None, country_code: str = ""):
    """Send a WhatsApp template message."""
    phone = _format_wa_phone(phone, country_code)

    tmpl_info = WA_TEMPLATES.get(template_name)
    if not tmpl_info:
        log.error("WhatsApp template '%s' not registered", template_name)
        return False, f"Template '{template_name}' not registered"

    lang = tmpl_info["lang"]
    expected_params = tmpl_info["params"]

    if body_params and len(body_params) != expected_params:
        log.warning("WhatsApp template '%s' expects %d params, got %d — adjusting",
                     template_name, expected_params, len(body_params))
        body_params = body_params[:expected_params]

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang},
        }
    }

    if body_params:
        payload["template"]["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in body_params]
        }]

    return _wa_send(phone, payload)


MSG91_WA_URL = "https://api.msg91.com/api/v5/whatsapp/whatsapp-outbound-message/bulk/"
MSG91_WA_TEMPLATE_URL = "https://control.msg91.com/api/v5/whatsapp/get-template/{number}/"


def msg91_wa_templates():
    """Approved WhatsApp templates on the integrated number, as
    {name: {"status": str, "lang": str, "params": int}}. The trailing slash is
    required — without it msg91 redirects to an internal host and the authkey is
    dropped."""
    if not config.MSG91_AUTHKEY or not config.MSG91_INTEGRATED_NUMBER:
        return {}
    url = MSG91_WA_TEMPLATE_URL.format(number=config.MSG91_INTEGRATED_NUMBER)
    req = Request(url, headers={"authkey": config.MSG91_AUTHKEY,
                               "Content-Type": "application/json"})
    out = {}
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        log.warning("msg91 template list failed: %s", e)
        return {}
    for tmpl in data.get("data") or []:
        for lang in tmpl.get("languages") or []:
            out[tmpl.get("name")] = {"status": lang.get("status"),
                                     "lang": lang.get("language"),
                                     "params": len(lang.get("variables") or []),
                                     "namespace": tmpl.get("namespace")}
    return out


def send_msg91_wa_template(phone: str, template_name: str, body_values: list,
                           lang: str = "en", country_code: str = "IN"):
    """Send a WhatsApp template via msg91 (Optiwar WABA).
    body_values fill body_1..body_N of the approved template. Returns (ok, msg)."""
    if not config.MSG91_AUTHKEY:
        log.warning("msg91 WhatsApp not configured (missing MSG91_AUTHKEY)")
        return False, "msg91 WhatsApp not configured"
    to = _format_wa_phone(phone, country_code)
    components = {}
    for i, val in enumerate(body_values or [], start=1):
        components[f"body_{i}"] = {"type": "text", "value": str(val)}
    payload = {
        "integrated_number": config.MSG91_INTEGRATED_NUMBER,
        "content_type": "template",
        "payload": {
            "messaging_product": "whatsapp",
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": lang, "policy": "deterministic"},
                "namespace": config.MSG91_WA_NAMESPACE,
                "to_and_components": [{"to": [to], "components": components}],
            },
        },
    }
    data = json.dumps(payload).encode()
    req = Request(MSG91_WA_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("authkey", config.MSG91_AUTHKEY)
    log.info("msg91 WhatsApp -> %s tmpl=%s", to, template_name)
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
        result = json.loads(body) if body else {}
        status = str(result.get("status", result.get("type", ""))).lower()
        if status and status not in ("error", "fail", "failure"):
            return True, f"WhatsApp (msg91) sent: {json.dumps(result)[:200]}"
        # Some msg91 responses are 200 with a request id and no status field
        if not status and result:
            return True, f"WhatsApp (msg91) sent: {json.dumps(result)[:200]}"
        return False, f"msg91 response: {json.dumps(result)[:300]}"
    except HTTPError as e:
        eb = e.read().decode()
        log.error("msg91 HTTP %d: %s", e.code, eb[:300])
        return False, f"msg91 HTTP {e.code}: {eb[:200]}"
    except URLError as e:
        return False, f"msg91 network error: {e}"
    except Exception as e:
        return False, f"msg91 error: {e}"


def send_wa_text(phone: str, message: str, country_code: str = ""):
    """Send a plain WhatsApp text message.
    NOTE: Only works within 24hr conversation window."""
    phone = _format_wa_phone(phone, country_code)
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": message}
    }
    log.info("WhatsApp TEXT (24hr window required) to %s", phone)
    return _wa_send(phone, payload)


# ─── SMS (Twilio) ────────────────────────────────────────────────────

def send_sms(to: str, body: str):
    """Send an SMS via Twilio REST API."""
    if not all([config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN, config.TWILIO_FROM_NUMBER]):
        log.warning("Twilio SMS not configured — skipping SMS to %s", to)
        return False, "Twilio SMS not configured"

    to = _format_sms_phone(to)
    url = f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json"
    data = f"To={to}&From={config.TWILIO_FROM_NUMBER}&Body={body}".encode()
    credentials = b64encode(f"{config.TWILIO_ACCOUNT_SID}:{config.TWILIO_AUTH_TOKEN}".encode()).decode()

    req = Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    log.info("SMS sending to %s", to)

    try:
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        sid = result.get("sid", "unknown")
        log.info("SMS SENT to %s — sid: %s", to, sid)
        return True, f"SMS sent (sid: {sid})"
    except HTTPError as e:
        err_body = e.read().decode()
        log.error("SMS HTTP %d to %s: %s", e.code, to, err_body[:300])
        return False, f"SMS error {e.code}: {err_body[:200]}"
    except URLError as e:
        log.error("SMS network error to %s: %s", to, e)
        return False, f"SMS network error: {e}"
    except Exception as e:
        log.error("SMS error to %s: %s", to, e)
        return False, str(e)


def _format_sms_phone(phone: str, country_code: str = "") -> str:
    """Format phone for Twilio (E.164 with + prefix)."""
    # Reuse WhatsApp formatter for consistent E.164, then add +
    digits = _format_wa_phone(phone, country_code)
    if not digits.startswith("+"):
        digits = "+" + digits
    return digits


# ─── Unified Notify Functions ────────────────────────────────────────

def notify_order_shipped(order: dict, tracking: str, courier: str = "FedEx",
                         channels: list = None):
    """Send shipment notification via selected channels.
    Mirrors PHP: sends orders_shipped template with (order_id, awb)."""
    if channels is None:
        channels = ["email"]

    results = {}
    email = order.get("email", "")
    phone = order.get("telephone", "")
    order_id = order.get("order_id")
    name = f"{order.get('firstname', '')} {order.get('lastname', '')}".strip()

    log.info("notify_order_shipped #%s channels=%s email=%s phone=%s",
             order_id, channels, email, phone)

    if "email" in channels and email:
        subject = f"Your order #E-{order_id} has been shipped!"
        html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #16a34a;">Your order has been shipped!</h2>
            <p>Hi {name},</p>
            <p>Order #E-{order_id} has been dispatched via {courier}.</p>
            <div style="background: #f0fdf4; padding: 15px; border-radius: 8px; margin: 15px 0;">
                <strong>Tracking Number:</strong> {tracking}<br>
                <strong>Courier:</strong> {courier}
            </div>
            <p>Track your shipment at:
                <a href="https://www.fedex.com/fedextrack/?trknbr={tracking}">FedEx Tracking</a>
            </p>
            <p>Thank you for shopping with LensBazaar Europe!</p>
        </div>
        """
        text = f"Order #E-{order_id} shipped via {courier}. Tracking: {tracking}"
        results["email"] = send_email(email, subject, html, text)

    shipping_cc = order.get("shipping_country_code", "") or order.get("payment_country_code", "")
    if "whatsapp" in channels and phone:
        # EU: use orders_shipped_eu with order_id and AWB
        results["whatsapp"] = send_wa_template(phone, "orders_shipped_eu",
                                                body_params=[f"E-{order_id}", tracking],
                                                country_code=shipping_cc)

    if "sms" in channels and phone:
        msg = f"LensBazaar: Order #E-{order_id} shipped via {courier}. Tracking: {tracking}. Track at fedex.com/fedextrack/?trknbr={tracking}"
        results["sms"] = send_sms(phone, msg)

    recipients = {"email": email, "whatsapp": phone, "sms": phone}
    _log_results("order_shipped", order_id, results, recipients)
    return results


PROCESSED_STATUS_ID = 15


def _order_success_wa_sent(order_id) -> bool:
    """True if checkout success / the reconciler already sent the order_success
    WhatsApp for this EU order (so a Processed status change must not repeat it)."""
    try:
        from database import db_cursor
        with db_cursor() as cur:
            cur.execute("""SELECT 1 FROM notification_logs
                           WHERE order_id = %s AND channel = 'whatsapp'
                             AND event = 'order_success' AND success = 1 LIMIT 1""", (order_id,))
            return cur.fetchone() is not None
    except Exception as e:
        log.error("order_success dedupe lookup failed for #%s: %s", order_id, e)
        return False


def notify_order_status(order: dict, new_status: str, comment: str = "",
                        channels: list = None, status_id: int = None):
    """Send order status update via selected channels.
    Uses status_id to map to correct WhatsApp template (matches PHP if-else chain)."""
    if channels is None:
        channels = ["email"]

    results = {}
    email = order.get("email", "")
    phone = order.get("telephone", "")
    shipping_cc = order.get("shipping_country_code", "") or order.get("payment_country_code", "")
    order_id = order.get("order_id")
    name = f"{order.get('firstname', '')} {order.get('lastname', '')}".strip()

    log.info("notify_order_status #%s status=%s(%s) channels=%s email=%s phone=%s",
             order_id, new_status, status_id, channels, email, phone)

    if "email" in channels and email:
        subject = f"Order #E-{order_id} status update: {new_status}"
        html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2>Order #E-{order_id} Update</h2>
            <p>Hi {name},</p>
            <p>Your order status has been updated to: <strong>{new_status}</strong></p>
            {"<p>Note: " + comment + "</p>" if comment else ""}
            <p>Thank you for shopping with LensBazaar Europe!</p>
        </div>
        """
        text = f"Order #E-{order_id} status: {new_status}. {comment}"
        results["email"] = send_email(email, subject, html, text)

    if "whatsapp" in channels and phone:
        # Look up template by status_id (matches PHP if-else chain)
        template_name = None
        if status_id is not None:
            template_name = STATUS_TEMPLATE_MAP.get(status_id)

        if template_name:
            tmpl_info = WA_TEMPLATES[template_name]
            if tmpl_info["params"] == 2:
                body_params = [f"E-{order_id}", comment or new_status]
            else:
                body_params = [f"E-{order_id}"]
            results["whatsapp"] = send_wa_template(phone, template_name,
                                                    body_params=body_params,
                                                    country_code=shipping_cc)
        elif status_id == PROCESSED_STATUS_ID and _order_success_wa_sent(order_id):
            results["whatsapp"] = (True, "skipped: order_success WhatsApp already sent for this order")
        else:
            # Use order_notify template for general status updates
            results["whatsapp"] = send_wa_template(phone, "order_notify",
                                                    body_params=[f"E-{order_id}"],
                                                    country_code=shipping_cc)

    if "sms" in channels and phone:
        msg = f"LensBazaar: Order #E-{order_id} updated to {new_status}. {comment}".strip()
        results["sms"] = send_sms(phone, msg)

    recipients = {"email": email, "whatsapp": phone, "sms": phone}
    _log_results(f"status_{new_status}", order_id, results, recipients)
    return results


def _log_results(func_name: str, order_id, results: dict, recipients: dict = None,
                  created_by: str = ""):
    """Log notification results to console and DB."""
    for ch, (ok, msg) in results.items():
        if ok:
            log.info("%s #%s [%s] SUCCESS: %s", func_name, order_id, ch, msg)
        else:
            log.error("%s #%s [%s] FAILED: %s", func_name, order_id, ch, msg)
        recipient = (recipients or {}).get(ch, "")
        _save_notification_log(order_id, ch, func_name, recipient, ok, msg, created_by)


def _ensure_notif_ow_column():
    try:
        from database import db_cursor
        with db_cursor() as cur:
            cur.execute("SHOW COLUMNS FROM notification_logs LIKE 'ow_order_id'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE notification_logs ADD COLUMN ow_order_id VARCHAR(50) NULL")
            cur.execute("SHOW COLUMNS FROM notification_logs LIKE 'reference'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE notification_logs ADD COLUMN reference VARCHAR(191) NULL")
    except Exception as e:
        log.warning("Could not ensure notification_logs columns: %s", e)


def _extract_reference(message: str) -> str:
    """Pull a provider delivery receipt/reference out of a send-result message so
    it can be logged as proof: msg91 request_id, Twilio SID, or SMTP message id."""
    if not message:
        return ""
    # Meta WhatsApp Cloud API message id (wamid....) — this is the delivery receipt
    m = re.search(r'(wamid\.[A-Za-z0-9_\-=]+)', message)
    if m:
        return m.group(1)
    # msg91 WhatsApp / SMS: {"request_id": "..."} or {"message_id": "..."}
    m = re.search(r'"(?:request_id|requestId|message_id)"\s*:\s*"([^"]+)"', message)
    if m:
        return m.group(1)
    # Twilio SID (SMS) e.g. SM32c... / MMxxxx
    m = re.search(r'\b([SM]M[0-9a-fA-F]{16,})\b', message)
    if m:
        return m.group(1)
    # Generic "... (id: XXXX)" fallback (e.g. "WhatsApp sent (id: ...)")
    m = re.search(r'\bid:\s*([^\s)]+)', message)
    if m:
        return m.group(1)
    return ""


def _split_order_id(order_id):
    """Return (numeric_id, ow_order_id). EU orders are numeric; Optiwar are strings."""
    try:
        return int(order_id), None
    except (TypeError, ValueError):
        return 0, str(order_id)


_DETAILS_MAX = 60000  # `details` is TEXT (64 KB) in both notification_logs tables


def html_to_text(html_body: str) -> str:
    """Plain-text rendering of a simple HTML email body."""
    import html as html_mod
    s = html_body or ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|h[1-6]|li|tr)>", "\n\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "- ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_mod.unescape(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def email_details(subject: str, body_html: str, followup_of=None) -> str:
    """JSON blob stored in notification_logs.details: subject + rich + text
    versions of an outgoing email, so the sent text is on record."""
    text = html_to_text(body_html)
    html_part = body_html or ""
    payload = {"subject": subject or "", "html": html_part, "text": text}
    if followup_of:
        payload["followup_of"] = int(followup_of)
    blob = json.dumps(payload, ensure_ascii=False)
    if len(blob.encode("utf-8")) > _DETAILS_MAX:
        payload["html"] = html_part[: max(0, _DETAILS_MAX - len(text) - 2000)] + "…[truncated]"
        payload["truncated"] = True
        blob = json.dumps(payload, ensure_ascii=False)
    return blob


def _save_notification_log(order_id, channel, event, recipient, success, message,
                           created_by="", details=None):
    """Persist notification result to notification_logs table.
    Supports numeric EU order ids and string Optiwar order ids (ow_order_id).
    Returns the new log id (or None on failure)."""
    numeric, ow_id = _split_order_id(order_id)
    reference = _extract_reference(message)
    try:
        from database import db_cursor, ow_ops_cursor
        if ow_id is not None:
            with ow_ops_cursor() as cur:
                cur.execute("""
                    INSERT INTO ops_notification_logs
                        (ow_order_id, channel, event, recipient, success, message, details, reference, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (ow_id, channel, event, recipient, 1 if success else 0,
                      (message or "")[:500], details, reference, created_by))
                return cur.lastrowid
        else:
            _ensure_notif_ow_column()
            with db_cursor() as cur:
                cur.execute("""
                    INSERT INTO notification_logs
                        (order_id, channel, event, recipient, success, message, details, reference, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (numeric, channel, event, recipient, 1 if success else 0,
                      (message or "")[:500], details, reference, created_by))
                return cur.lastrowid
    except Exception as e:
        log.error("Failed to save notification log: %s", e)
        return None


def get_notification_log(order_id, log_id: int):
    """One notification row (with details) for an order, or None."""
    numeric, ow_id = _split_order_id(order_id)
    try:
        from database import db_cursor, ow_cursor
        if ow_id is not None:
            with ow_cursor() as cur:
                cur.execute("""SELECT id, channel, event, recipient, success, message, details,
                                      reference, created_by, created_at
                               FROM ops_notification_logs WHERE id = %s AND ow_order_id = %s""",
                            (log_id, ow_id))
                return cur.fetchone()
        with db_cursor() as cur:
            cur.execute("""SELECT id, channel, event, recipient, success, message, details,
                                  reference, created_by, created_at
                           FROM notification_logs WHERE id = %s AND order_id = %s""",
                        (log_id, numeric))
            return cur.fetchone()
    except Exception as e:
        log.error("Failed to fetch notification log %s: %s", log_id, e)
        return None


def parse_email_details(row) -> dict:
    """Decode the details JSON of a support-email log row into
    {subject, html, text, sent_at, to}; empty dict when absent."""
    if not row:
        return {}
    d = None
    if row.get("details"):
        try:
            d = json.loads(row["details"])
        except (TypeError, ValueError):
            d = None
    if not isinstance(d, dict):
        # Rows logged before bodies were stored: recover the subject from
        # the summary line ("... Subject: <subject>") so a follow-up can
        # still reference the earlier email.
        m = re.search(r"Subject:\s*(.+?)\s*$", row.get("message") or "")
        d = {"subject": m.group(1) if m else "", "html": "", "text": "",
             "no_body": True}
    d.setdefault("text", html_to_text(d.get("html", "")))
    d["sent_at"] = str(row.get("created_at") or "")
    d["to"] = row.get("recipient") or ""
    d["id"] = row.get("id")
    return d


def get_notification_logs(order_id) -> list:
    """Fetch notification logs for an order (EU numeric or Optiwar string), newest first."""
    numeric, ow_id = _split_order_id(order_id)
    try:
        from database import db_cursor, ow_cursor
        if ow_id is not None:
            with ow_cursor() as cur:
                cur.execute("""
                    SELECT id, channel, event, recipient, success, message,
                           reference, created_by, created_at
                    FROM ops_notification_logs
                    WHERE ow_order_id = %s
                    ORDER BY created_at DESC LIMIT 50
                """, (ow_id,))
                return cur.fetchall()
        _ensure_notif_ow_column()
        with db_cursor() as cur:
            cur.execute("""
                SELECT id, channel, event, recipient, success, message,
                       reference, created_by, created_at
                FROM notification_logs
                WHERE order_id = %s
                ORDER BY created_at DESC LIMIT 50
            """, (numeric,))
            return cur.fetchall()
    except Exception as e:
        log.error("Failed to fetch notification logs: %s", e)
        return []


def notify_order_cancelled(order: dict, channels: list = None):
    return notify_order_status(order, "Canceled", "Your order has been cancelled.",
                               channels, status_id=7)


def notify_order_refunded(order: dict, channels: list = None):
    return notify_order_status(order, "Refunded", "Your order has been refunded.",
                               channels, status_id=11)


EU_ADMIN_EMAIL = "admin@lensbazaar.com"


def notify_order_refund(order: dict, refund_eur: float = 0.0, refund_inr: float = 0.0,
                        reason: str = "", refund_type: str = "full",
                        channels: list = None, email_subject: str = None,
                        email_html: str = None, email_text: str = None,
                        admin_subject: str = None, admin_html: str = None,
                        admin_text: str = None, dry_run: bool = False):
    """Notify an EU customer that a refund (full or partial) was processed.

    The CUSTOMER email carries EUR-only figures (no INR). A SEPARATE internal email
    to admin@lensbazaar.com carries the full conversion detail (INR base, deductions,
    amount pushed to Razorpay + EUR equivalents) — passed in via admin_html/admin_text.
    WhatsApp fires the approved Meta template ``order_refunded`` (order_id only — it
    directs the customer to the email for the snapshot). SMS carries the amount.
    All channels are logged with provider receipts.
    """
    if channels is None:
        channels = ["email", "whatsapp", "sms"]

    email = order.get("email", "")
    phone = order.get("telephone", "")
    shipping_cc = order.get("shipping_country_code", "") or order.get("payment_country_code", "")
    order_id = order.get("order_id")
    name = f"{order.get('firstname', '')} {order.get('lastname', '')}".strip()
    label = "Refund" if refund_type == "full" else "Partial refund"

    subject = email_subject or f"{label} processed for your order #E-{order_id}"
    if not email_html:
        email_html = (
            f'<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">'
            f'<h2>Order #E-{order_id} — {label}</h2>'
            f'<p>Dear {_esc(name)},</p>'
            f'<p>A {label.lower()} of <b>&euro;{refund_eur:.2f}</b> has been processed for your '
            f'order <b>#E-{order_id}</b> and returned to your original payment method.</p>'
            + (f'<p>Reason: {_esc(reason)}</p>' if reason else '')
            + '<p>Please allow 5-7 business days for it to reflect with your bank.</p>'
            '<p>Best regards,<br><strong>LensBazaar Customer Support</strong></p></div>'
        )
    if not email_text:
        email_text = (f"Dear {name}, a {label.lower()} of EUR {refund_eur:.2f} has been processed "
                      f"for your order #E-{order_id}. Please allow 5-7 business days. "
                      "— LensBazaar Customer Support")

    admin_subject = admin_subject or f"[Internal] {label} to book — #E-{order_id}"

    if dry_run:
        preview = {"dry_run": True, "refund_type": refund_type,
                   "refund_eur": refund_eur, "refund_inr": refund_inr}
        if "email" in channels:
            preview["email"] = {"from": config.SMTP_FROM or config.SMTP_USER, "to": email,
                                "subject": subject, "html": email_html}
        if "whatsapp" in channels:
            preview["whatsapp"] = {"to": phone, "template": "order_refunded",
                                   "params": [f"E-{order_id}"], "country_code": shipping_cc}
        if "sms" in channels:
            preview["sms"] = {"to": phone}
        if admin_html:
            preview["admin"] = {"from": config.SMTP_FROM or config.SMTP_USER,
                                "to": EU_ADMIN_EMAIL, "subject": admin_subject,
                                "html": admin_html}
        preview["text"] = email_text
        return preview

    results = {}
    if "email" in channels and email:
        results["email"] = send_email(email, subject, email_html, email_text, admin_cc=None)
    if "whatsapp" in channels and phone:
        results["whatsapp"] = send_wa_template(phone, "order_refunded",
                                               body_params=[f"E-{order_id}"],
                                               country_code=shipping_cc)
    if "sms" in channels and phone:
        results["sms"] = send_sms(phone, email_text)
    if admin_html:
        results["admin"] = send_email(EU_ADMIN_EMAIL, admin_subject, admin_html,
                                       admin_text, admin_cc=None)

    recipients = {"email": email, "whatsapp": phone, "sms": phone, "admin": EU_ADMIN_EMAIL}
    _log_results(f"refund_{refund_type}", order_id, results, recipients)
    return results


# ─── Legacy wrappers (backward compatibility) ────────────────────────

def send_shipment_notification(to: str, order_id: int, tracking: str, courier: str = "FedEx"):
    subject = f"Your order #E-{order_id} has been shipped!"
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #16a34a;">Your order has been shipped!</h2>
        <p>Order #E-{order_id} has been dispatched via {courier}.</p>
        <div style="background: #f0fdf4; padding: 15px; border-radius: 8px; margin: 15px 0;">
            <strong>Tracking Number:</strong> {tracking}<br>
            <strong>Courier:</strong> {courier}
        </div>
        <p>Track your shipment at:
            <a href="https://www.fedex.com/fedextrack/?trknbr={tracking}">FedEx Tracking</a>
        </p>
        <p>Thank you for shopping with LensBazaar Europe!</p>
    </div>
    """
    text = f"Order #E-{order_id} shipped via {courier}. Tracking: {tracking}"
    return send_email(to, subject, html, text)


def send_order_status_email(to: str, order_id: int, new_status: str, comment: str = ""):
    subject = f"Order #E-{order_id} status update: {new_status}"
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2>Order #E-{order_id} Update</h2>
        <p>Your order status has been updated to: <strong>{new_status}</strong></p>
        {"<p>Note: " + comment + "</p>" if comment else ""}
        <p>Thank you for shopping with LensBazaar Europe!</p>
    </div>
    """
    return send_email(to, subject, html)


# ─── Optiwar (Delhivery) shipment notification ───────────────────────

# Fixed Optiwar house style
OW_BRAND = "#0b5cab"
OW_ACCENT = "#16a34a"
# Generic msg91 WhatsApp template for manual status changes that have no dedicated
# template (e.g. Shipped without an AWB). Body vars: 1=name, 2=OW-order, 3=status.
OW_STATUS_WA_TEMPLATE = "ow_status"


def _esc(v) -> str:
    return html_escape("" if v is None else str(v))


def _rx_prescription_table(rx: dict, title: str = "Eye Prescription",
                          muted: bool = False) -> str:
    """Render right_eye/left_eye powers as a styled eye-prescription table.
    Values are stored as 'SPH/CYL/AXIS/ADD' (slash-separated)."""
    if not rx:
        return ""
    right = (rx.get("right_eye") or "").strip()
    left = (rx.get("left_eye") or "").strip()
    if not right and not left:
        return ""
    labels = ["SPH", "CYL", "AXIS", "ADD"]

    def cells(val):
        formatted = ow_rx.format_eye(val)
        parts = ([formatted[key.lower()] for key in labels] if formatted else
                 ([p.strip() for p in val.split("/")] if val else []))
        out = ""
        for i in range(4):
            v = parts[i] if i < len(parts) else "—"
            out += f"<td style='padding:5px 10px;border:1px solid #dbe4ef;text-align:center;'>{_esc(v or '—')}</td>"
        return out

    border = "#e5e7eb" if muted else "#cfe0f3"
    head_bg = "#f8fafc" if muted else "#eef4fb"
    title_color = "#64748b" if muted else OW_BRAND
    head = "".join(
        f"<th style='padding:5px 10px;border:1px solid {border};background:{head_bg};font-size:11px;'>{l}</th>"
        for l in labels)
    rows = ""
    if right:
        rows += ("<tr><td style='padding:5px 10px;border:1px solid #dbe4ef;font-weight:700;background:#f7fafc;'>Right (OD)</td>"
                 + cells(right) + "</tr>")
    if left:
        rows += ("<tr><td style='padding:5px 10px;border:1px solid #dbe4ef;font-weight:700;background:#f7fafc;'>Left (OS)</td>"
                 + cells(left) + "</tr>")
    return f"""
    <div style="margin-top:8px;">
        <div style="font-size:11px;font-weight:700;color:{title_color};text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px;">{_esc(title)}</div>
        <table style="border-collapse:collapse;font-size:{'11px' if muted else '12px'};{'color:#64748b;' if muted else ''}">
            <thead><tr>
                <th style="padding:5px 10px;border:1px solid {border};background:{head_bg};font-size:11px;text-align:left;">Eye</th>
                {head}
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""


def build_ow_rx_changed_email(order: dict, old_rx: dict, new_rx: dict):
    """Build the customer email for a confirmed Optiwar prescription change."""
    order_id = order.get("order_id")
    name = (order.get("firstname") or "").strip() or "Customer"
    subject = f"Your prescription for Optiwar order OW-{order_id} has been updated"
    intro = (
        f"as you asked, we've updated the prescription on your order OW-{order_id} "
        "before your lenses are made. Please check the new values below. If anything "
        "is not right, just reply to this email or message us on WhatsApp before we "
        "start making your lenses."
    )
    new_table = _rx_prescription_table(new_rx, title="New prescription")
    old_table = _rx_prescription_table(
        old_rx, title="Previous prescription", muted=True)
    html = (
        f"<div style='font-family:Segoe UI,Arial,sans-serif;max-width:560px;margin:0 auto;'>"
        f"<div style='background:{OW_ACCENT};color:#fff;padding:14px 18px;border-radius:8px 8px 0 0;'>"
        f"<b>Optiwar</b> — Prescription update</div>"
        f"<div style='border:1px solid #eee;border-top:0;padding:18px;border-radius:0 0 8px 8px;'>"
        f"<p>Hi {_esc(name)}, {_esc(intro)}</p>"
        f"{new_table}{old_table}"
        f"<p style='margin-top:16px;'>— Optiwar Team</p></div></div>"
    )
    text = (
        f"Hi {name}, {intro}\n\nNew prescription\n"
        f"{_rx_prescription_text(new_rx)}\n\nPrevious prescription\n"
        f"{_rx_prescription_text(old_rx)}\n\n— Optiwar Team"
    )
    return subject, html, text


def _rx_prescription_text(rx: dict) -> str:
    if not rx:
        return ""
    rows = []
    for label, key in (("Right (OD)", "right_eye"), ("Left (OS)", "left_eye")):
        raw = str(rx.get(key) or "").strip()
        if not raw:
            continue
        formatted = ow_rx.format_eye(raw)
        if formatted:
            values = [formatted[field] for field in ("sph", "cyl", "axis", "add")]
        else:
            values = [part.strip() or "—" for part in raw.split("/")]
            values = (values + ["—"] * 4)[:4]
        rows.append(f"{label}: " + " | ".join(values))
    return "\n".join(rows)


def notify_ow_rx_changed(order: dict, old_rx: dict, new_rx: dict,
                         channels: list = None):
    """Send the approved prescription-change notice through selected EWS channels."""
    channels = channels or []
    subject, html, text = build_ow_rx_changed_email(order, old_rx, new_rx)
    email = (order.get("email") or "").strip()
    phone = (order.get("telephone") or "").strip()
    order_id = order.get("order_id")
    name = (order.get("firstname") or "").strip() or "Customer"
    results = {}

    if "email" in channels:
        if email:
            results["email"] = send_email(
                email, subject, html, text,
                from_addr=config.OPTIWAR_FROM_EMAIL,
                admin_cc=config.OPTIWAR_ADMIN_CC or None,
                smtp_host=config.OPTIWAR_SMTP_HOST or None,
                smtp_port=config.OPTIWAR_SMTP_PORT or None,
                smtp_user=config.OPTIWAR_SMTP_USER or None,
                smtp_pass=config.OPTIWAR_SMTP_PASS or None)
        else:
            results["email"] = (False, "No customer email on order")
    if "whatsapp" in channels:
        if phone:
            results["whatsapp"] = send_msg91_wa_template(
                phone, OW_STATUS_WA_TEMPLATE,
                body_values=[name, f"OW-{order_id}", "Prescription updated"],
                lang="en", country_code="IN")
        else:
            results["whatsapp"] = (False, "No customer phone on order")
    if "sms" in channels:
        if phone:
            sms_text = (
                f"Hi {name}, the prescription on your Optiwar order OW-{order_id} "
                "has been updated as you asked. Please check the email we sent and "
                "reply if anything is not right. - Optiwar"
            )
            results["sms"] = send_sms(phone, sms_text)
        else:
            results["sms"] = (False, "No customer phone on order")

    _log_results(
        "ow_rx_changed", order_id, results,
        {"email": email, "whatsapp": phone, "sms": phone})
    return results


def _ai_change_summary(order: dict, changes: list, note: str) -> str:
    """Use DeepSeek to compose a short, warm HTML paragraph describing the
    approved order changes. Returns an HTML snippet; falls back to a template."""
    name = (order.get("firstname", "") or "").strip() or "there"
    change_lines = "\n".join(
        f"- {c.get('item','')} changed to {c.get('changed_to','')} "
        f"(customer approved: {c.get('approved','')})"
        for c in (changes or [])
    )
    fallback = ""
    if changes:
        approved_all = all((c.get("approved", "").lower() == "yes") for c in changes)
        lead = ("As discussed and approved by you, we made the following adjustment"
                + ("s" if len(changes) > 1 else "") + " to your lenses before dispatch:")
        fallback = f"<p>Hi {_esc(name)},</p><p>{lead}</p>"
    if not changes:
        return f"<p>Hi {_esc(name)},</p><p>Good news — your order is on its way!</p>"

    api_key = getattr(config, "DEEPSEEK_API_KEY", "")
    if not api_key:
        return fallback
    try:
        system_prompt = (
            "You write concise, warm dispatch-confirmation copy for Optiwar, an online "
            "eyewear/contact-lens retailer. Output ONLY HTML paragraphs (<p>...</p>), no "
            "headings, no subject, no CSS. 2 short paragraphs max. Address the customer by "
            "first name. Confirm the order is shipping and clearly state the change(s) that "
            "were made and that the customer approved them. Professional and friendly.")
        user_prompt = (
            f"Customer first name: {name}\n"
            f"Order: OW-{order.get('order_id','')}\n"
            f"Approved change(s):\n{change_lines}\n"
            f"Operator note: {note or '(none)'}\n"
            "Write the confirmation copy.")
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat",
                  "messages": [{"role": "system", "content": system_prompt},
                               {"role": "user", "content": user_prompt}],
                  "temperature": 0.5, "max_tokens": 500},
            timeout=30)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        if "<p" not in content:
            content = "<p>" + content.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
        return content
    except Exception as e:
        log.warning("DeepSeek change-summary failed: %s", e)
        return fallback


def build_ow_shipped_email(order: dict, tracking: str, courier: str = "Delhivery",
                           changes: list = None, note: str = "", use_ai: bool = True):
    """Build (subject, html, text) for an Optiwar order-shipped customer email,
    including any operator-entered order changes + customer approval."""
    changes = changes or []
    order_id = order.get("order_id", "")
    name = (order.get("firstname", "") or "").strip() or "Customer"
    track_url = config.courier_track_url(courier, _esc(tracking))

    # Products + original Rx recommendations (from rx_collector via order.products[].rx)
    prod_rows = ""
    for p in order.get("products", []):
        r = p.get("rx") or {}
        rec = r.get("recommendations") or ""
        rx_html = ""
        if rec:
            rx_html += f"<br><span style='color:#555;font-size:12px;'>Selection: {_esc(rec)}</span>"
        rx_html += _rx_prescription_table(r)
        prod_rows += (
            "<tr>"
            f"<td style='padding:7px 8px;border-bottom:1px solid #eee;'>{_esc(p.get('name',''))}{rx_html}</td>"
            f"<td style='padding:7px 8px;border-bottom:1px solid #eee;text-align:center;vertical-align:top;'>{_esc(p.get('model',''))}</td>"
            f"<td style='padding:7px 8px;border-bottom:1px solid #eee;text-align:center;vertical-align:top;'>{_esc(p.get('quantity',''))}</td>"
            "</tr>"
        )

    # Order-changes table
    changes_block = ""
    if changes:
        crows = ""
        for c in changes:
            approved = (c.get("approved") or "").strip()
            badge_color = "#16a34a" if approved.lower() == "yes" else "#b42318"
            crows += (
                "<tr>"
                f"<td style='padding:7px 8px;border-bottom:1px solid #eee;'>{_esc(c.get('item',''))}</td>"
                f"<td style='padding:7px 8px;border-bottom:1px solid #eee;'><strong>{_esc(c.get('changed_to',''))}</strong></td>"
                f"<td style='padding:7px 8px;border-bottom:1px solid #eee;text-align:center;'>"
                f"<span style='color:{badge_color};font-weight:700;'>{_esc(approved or '—')}</span></td>"
                "</tr>"
            )
        changes_block = f"""
        <h3 style="margin:20px 0 6px; color:{OW_BRAND};">Changes to Your Order</h3>
        <table style="border-collapse:collapse; width:100%; font-size:13px;">
            <thead>
                <tr style="background:#eef4fb;">
                    <th style="text-align:left;padding:7px 8px;border-bottom:2px solid #cfe0f3;">Original Selection</th>
                    <th style="text-align:left;padding:7px 8px;border-bottom:2px solid #cfe0f3;">Changed To</th>
                    <th style="text-align:center;padding:7px 8px;border-bottom:2px solid #cfe0f3;">Customer Approved</th>
                </tr>
            </thead>
            <tbody>{crows}</tbody>
        </table>"""

    intro = _ai_change_summary(order, changes, note) if use_ai else (
        f"<p>Hi {_esc(name)},</p><p>Your order is on its way!</p>")

    note_block = ""
    if note:
        note_block = (f"<p style='font-size:12px;color:#555;margin-top:10px;'>"
                      f"<strong>Note:</strong> {_esc(note)}</p>")

    ship_to = ", ".join(x for x in [
        _esc(order.get("shipping_address_1", "")), _esc(order.get("shipping_zone", "")),
        _esc(order.get("shipping_postcode", "")), _esc(order.get("shipping_country", "")),
    ] if x)

    subject = f"Your Optiwar order OW-{order_id} has shipped — AWB {tracking}"
    html = f"""
    <div style="font-family: Arial, Helvetica, sans-serif; max-width: 640px; margin: 0 auto; color:#1a1a1a;">
        <div style="background:{OW_BRAND};padding:16px 22px;border-radius:8px 8px 0 0;">
            <span style="color:#fff;font-size:22px;font-weight:800;letter-spacing:1px;">OPTIWAR</span>
            <span style="color:#cfe0f3;font-size:12px;margin-left:8px;">Dispatch Confirmation</span>
        </div>
        <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:22px;">
            <div style="line-height:1.6;">{intro}</div>
            <p style="margin:14px 0 4px;">Your order <strong>OW-{_esc(order_id)}</strong> has been dispatched via <strong>{_esc(courier)}</strong>.</p>
            <div style="background:#f0fdf4; border:1px solid #bbf7d0; padding:14px 16px; border-radius:8px; margin:14px 0;">
                <div style="font-size:13px;color:#555;">Tracking / AWB Number</div>
                <div style="font-size:20px; font-weight:800; letter-spacing:1px;">{_esc(tracking)}</div>
                <div style="margin-top:10px;">
                    <a href="{track_url}" style="background:{OW_ACCENT};color:#fff;text-decoration:none;padding:9px 18px;border-radius:6px;font-weight:700;display:inline-block;">Track your shipment</a>
                </div>
            </div>
            {changes_block}
            <h3 style="margin:20px 0 6px; color:{OW_BRAND};">Order Details</h3>
            <table style="border-collapse:collapse; width:100%; font-size:13px;">
                <thead>
                    <tr style="background:#f6f8fa;">
                        <th style="text-align:left;padding:7px 8px;border-bottom:2px solid #ddd;">Product</th>
                        <th style="text-align:center;padding:7px 8px;border-bottom:2px solid #ddd;">Code</th>
                        <th style="text-align:center;padding:7px 8px;border-bottom:2px solid #ddd;">Qty</th>
                    </tr>
                </thead>
                <tbody>{prod_rows}</tbody>
            </table>
            {note_block}
            <p style="margin-top:14px;font-size:13px;"><strong>Shipping to:</strong> {ship_to}</p>
            <p style="margin-top:16px;">Thank you for choosing Optiwar!</p>
            <hr style="border:none;border-top:1px solid #eee;margin-top:22px;">
            <p style="color:#94a3b8;font-size:11px;">Automated dispatch notification from admin@optiwar.com.</p>
        </div>
    </div>
    """
    # Plain-text fallback
    ctext = ""
    if changes:
        ctext = " Changes: " + "; ".join(
            f"{c.get('item','')} -> {c.get('changed_to','')} (approved: {c.get('approved','')})"
            for c in changes)
    text = (f"Hi {name}, your Optiwar order OW-{order_id} has shipped via {courier}. "
            f"AWB/Tracking: {tracking}. Track: {track_url}.{ctext}")
    return subject, html, text


def notify_ow_status(order: dict, status_name: str, comment: str = "",
                     channels: list = None, tracking: str = "", eta_days: str = "5-7",
                     courier: str = "Delhivery"):
    """Notify an Optiwar customer of a manual status change (email + WhatsApp + SMS),
    logging every channel like the EU flow. WhatsApp uses the rich order_shipped
    template when status is 'Shipped' AND an AWB exists, otherwise the generic
    ow_status template so the customer is still messaged."""
    if channels is None:
        channels = []
    # Shipped with an AWB → reuse the full shipped notification (email + WhatsApp).
    if status_name.strip().lower() == "shipped" and tracking:
        return notify_ow_shipped(order, tracking, courier=courier or "Delhivery",
                                 changes=None, note=comment, use_ai=False,
                                 channels=channels, eta_days=eta_days)

    email = (order.get("email") or "").strip()
    phone = (order.get("telephone") or "").strip()
    order_id = order.get("order_id")
    name = (order.get("firstname", "") or "").strip() or "Customer"
    from_addr = config.OPTIWAR_FROM_EMAIL

    subject = f"Update on your Optiwar order OW-{order_id}: {status_name}"
    body_line = _esc(comment) if comment else f"Your order status is now <b>{_esc(status_name)}</b>."
    html = (f"<div style='font-family:Segoe UI,Arial,sans-serif;max-width:560px;margin:0 auto;'>"
            f"<div style='background:{OW_ACCENT};color:#fff;padding:14px 18px;border-radius:8px 8px 0 0;'>"
            f"<b>Optiwar</b> — Order Update</div>"
            f"<div style='border:1px solid #eee;border-top:0;padding:18px;border-radius:0 0 8px 8px;'>"
            f"<p>Hi {_esc(name)},</p><p>{body_line}</p>"
            f"<p style='color:#555;font-size:13px;'>Order: OW-{_esc(str(order_id))}<br>Status: {_esc(status_name)}</p>"
            f"<p style='margin-top:16px;'>— Optiwar Team</p></div></div>")
    text = f"Hi {name}, your Optiwar order OW-{order_id} status is now {status_name}. {comment}".strip()

    results = {}
    if "email" in channels:
        if email:
            results["email"] = send_email(
                email, subject, html, text, from_addr=from_addr,
                admin_cc=config.OPTIWAR_ADMIN_CC or None,
                smtp_host=config.OPTIWAR_SMTP_HOST or None,
                smtp_port=config.OPTIWAR_SMTP_PORT or None,
                smtp_user=config.OPTIWAR_SMTP_USER or None,
                smtp_pass=config.OPTIWAR_SMTP_PASS or None)
        else:
            results["email"] = (False, "No customer email on order")
    if "whatsapp" in channels:
        if phone:
            # Generic status template (msg91 'ow_status'). body: 1=name, 2=order, 3=status.
            results["whatsapp"] = send_msg91_wa_template(
                phone, OW_STATUS_WA_TEMPLATE,
                body_values=[name, f"OW-{order_id}", status_name],
                lang="en", country_code="IN")
        else:
            results["whatsapp"] = (False, "No customer phone on order")
    if "sms" in channels:
        if phone:
            results["sms"] = send_sms(phone, text)
        else:
            results["sms"] = (False, "No customer phone on order")

    _log_results("ow_status_update", order_id, results, {"email": email, "whatsapp": phone, "sms": phone})
    return results


def notify_ow_partial_refund(order: dict, amount: str = "", reason: str = "",
                             channels: list = None, dry_run: bool = False):
    """Notify an Optiwar customer that a partial refund was issued while the order
    continues. Email (styled), WhatsApp (order_partial_refund template), SMS.
    body params: 1=name, 2=amount, 3=OW-order id, 4=reason."""
    if channels is None:
        channels = []
    email = (order.get("email") or "").strip()
    phone = (order.get("telephone") or "").strip()
    order_id = order.get("order_id")
    name = (order.get("firstname", "") or "").strip() or "Customer"
    from_addr = config.OPTIWAR_FROM_EMAIL
    amt = (amount or "").strip() or "—"
    why = (reason or "").strip()

    subject = f"Partial refund on your Optiwar order OW-{order_id}"
    reason_html = f"<p style='color:#555;'>Reason: {_esc(why)}</p>" if why else ""
    html = (f"<div style='font-family:Segoe UI,Arial,sans-serif;max-width:560px;margin:0 auto;'>"
            f"<div style='background:{OW_ACCENT};color:#fff;padding:14px 18px;border-radius:8px 8px 0 0;'>"
            f"<b>Optiwar</b> — Partial Refund</div>"
            f"<div style='border:1px solid #eee;border-top:0;padding:18px;border-radius:0 0 8px 8px;'>"
            f"<p>Hi {_esc(name)},</p>"
            f"<p>A partial refund of <b>{_esc(amt)}</b> has been processed for your order "
            f"<b>OW-{_esc(str(order_id))}</b>.</p>"
            f"{reason_html}"
            f"<p>Your order remains on track and will be delivered as scheduled.</p>"
            f"<p style='margin-top:16px;'>— Optiwar Team</p></div></div>")
    text = (f"Hi {name}, a partial refund of {amt} has been processed for your order "
            f"OW-{order_id}." + (f" Reason: {why}." if why else "") +
            " Your order remains on track and will be delivered as scheduled. — Optiwar Team")

    if dry_run:
        return {
            "dry_run": True, "to": email or "(no email)", "from": from_addr,
            "cc": config.OPTIWAR_ADMIN_CC or "(none)", "subject": subject,
            "html": html, "text": text, "whatsapp_to": phone or "(no phone)",
            "wa_body": [name, amt, f"OW-{order_id}", why or "-"],
        }

    results = {}
    if "email" in channels:
        if email:
            results["email"] = send_email(
                email, subject, html, text, from_addr=from_addr,
                admin_cc=config.OPTIWAR_ADMIN_CC or None,
                smtp_host=config.OPTIWAR_SMTP_HOST or None,
                smtp_port=config.OPTIWAR_SMTP_PORT or None,
                smtp_user=config.OPTIWAR_SMTP_USER or None,
                smtp_pass=config.OPTIWAR_SMTP_PASS or None)
        else:
            results["email"] = (False, "No customer email on order")
    if "whatsapp" in channels:
        if phone:
            results["whatsapp"] = send_msg91_wa_template(
                phone, "ow_refunds",
                body_values=[name, amt, f"OW-{order_id}", why or "-"],
                lang="en", country_code="IN")
        else:
            results["whatsapp"] = (False, "No customer phone on order")
    if "sms" in channels:
        if phone:
            results["sms"] = send_sms(phone, text)
        else:
            results["sms"] = (False, "No customer phone on order")

    _log_results("ow_partial_refund", order_id, results, {"email": email, "whatsapp": phone, "sms": phone})
    return results


def _ai_upgrade_summary(order: dict, from_lens: str, to_lens: str, note: str) -> str:
    """DeepSeek-composed warm HTML paragraphs telling the customer their order was
    upgraded to a better lens free of charge. Falls back to a template on any error."""
    name = (order.get("firstname", "") or "").strip() or "there"
    fallback = (f"<p>Hi {_esc(name)},</p>"
                f"<p>Great news — as a complimentary gesture we've upgraded the lenses on your "
                f"order to <strong>{_esc(to_lens)}</strong>"
                + (f" (from {_esc(from_lens)})" if from_lens else "")
                + " at absolutely no extra cost to you.</p>"
                "<p>You'll receive the upgraded lenses with your order — no action is needed.</p>")
    api_key = getattr(config, "DEEPSEEK_API_KEY", "")
    if not api_key:
        return fallback
    try:
        system_prompt = (
            "You write concise, warm customer-notification copy for Optiwar, an online "
            "eyewear/contact-lens retailer. Output ONLY HTML paragraphs (<p>...</p>), no "
            "headings, no subject, no CSS. 2 short paragraphs max. Address the customer by "
            "first name. The message announces GOOD NEWS: their ordered complimentary lens "
            "has been upgraded FREE OF CHARGE to a better lens. Make clear it is at no extra "
            "cost, they need do nothing, and it ships with their order. Friendly, appreciative.")
        user_prompt = (
            f"Customer first name: {name}\n"
            f"Order: OW-{order.get('order_id','')}\n"
            f"Original lens: {from_lens or '(complimentary lens)'}\n"
            f"Upgraded free to: {to_lens}\n"
            f"Operator note: {note or '(none)'}\n"
            "Write the upgrade announcement copy.")
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat",
                  "messages": [{"role": "system", "content": system_prompt},
                               {"role": "user", "content": user_prompt}],
                  "temperature": 0.5, "max_tokens": 500},
            timeout=30)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        if "<p" not in content:
            content = "<p>" + content.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
        return content
    except Exception as e:
        log.warning("DeepSeek upgrade-summary failed: %s", e)
        return fallback


def build_ow_upgrade_email(order: dict, from_lens: str, to_lens: str,
                           note: str = "", use_ai: bool = True):
    """Build (subject, html, text) for the free-lens-upgrade customer email."""
    order_id = order.get("order_id", "")
    name = (order.get("firstname", "") or "").strip() or "Customer"
    intro = (_ai_upgrade_summary(order, from_lens, to_lens, note) if use_ai
             else f"<p>Hi {_esc(name)},</p><p>Your order lenses were upgraded free of charge.</p>")
    note_block = ""
    if note:
        note_block = (f"<p style='font-size:12px;color:#555;margin-top:10px;'>"
                      f"<strong>Note:</strong> {_esc(note)}</p>")
    from_row = (f"<div style='font-size:13px;color:#555;'>Originally ordered: "
                f"<span style='text-decoration:line-through;'>{_esc(from_lens)}</span></div>"
                if from_lens else "")
    subject = f"Good news — your Optiwar order OW-{order_id} was upgraded (free)"
    html = f"""
    <div style="font-family: Arial, Helvetica, sans-serif; max-width: 640px; margin: 0 auto; color:#1a1a1a;">
        <div style="background:{OW_BRAND};padding:16px 22px;border-radius:8px 8px 0 0;">
            <span style="color:#fff;font-size:22px;font-weight:800;letter-spacing:1px;">OPTIWAR</span>
            <span style="color:#cfe0f3;font-size:12px;margin-left:8px;">Complimentary Upgrade</span>
        </div>
        <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:22px;">
            <div style="line-height:1.6;">{intro}</div>
            <div style="background:#f0fdf4; border:1px solid #bbf7d0; padding:14px 16px; border-radius:8px; margin:16px 0;">
                <div style="font-size:12px;color:#166534;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">Free Upgrade Applied</div>
                {from_row}
                <div style="font-size:18px;font-weight:800;margin-top:4px;">{_esc(to_lens)}</div>
                <div style="font-size:12px;color:#16a34a;font-weight:700;margin-top:2px;">No extra charge</div>
            </div>
            <p style="margin:14px 0 4px;">Your order <strong>OW-{_esc(order_id)}</strong> will be dispatched with the upgraded lenses. No action is needed from your side.</p>
            {note_block}
            <p style="margin-top:16px;">Thank you for choosing Optiwar!</p>
            <hr style="border:none;border-top:1px solid #eee;margin-top:22px;">
            <p style="color:#94a3b8;font-size:11px;">Automated notification from admin@optiwar.com.</p>
        </div>
    </div>
    """
    text = (f"Hi {name}, good news — the lenses on your Optiwar order OW-{order_id} have been "
            f"upgraded FREE of charge"
            + (f" from {from_lens}" if from_lens else "")
            + f" to {to_lens}, at no extra cost. It ships with your order — no action needed. "
              "Thank you for choosing Optiwar!")
    return subject, html, text


def notify_ow_upgrade(order: dict, from_lens: str, to_lens: str, note: str = "",
                      channels: list = None, dry_run: bool = False,
                      subject: str = "", body_html: str = "", use_ai: bool = True):
    """Notify an Optiwar customer of a free lens upgrade (email + WhatsApp + SMS).
    If subject/body_html are supplied (operator-edited draft) they override the
    freshly-composed email. WhatsApp uses the 'ow_upgrade' msg91 template; if that
    template isn't approved yet msg91 will return an error which we log, while email
    + SMS still carry the full message."""
    if channels is None:
        channels = []
    email = (order.get("email") or "").strip()
    phone = (order.get("telephone") or "").strip()
    order_id = order.get("order_id")
    name = (order.get("firstname", "") or "").strip() or "Customer"
    from_addr = config.OPTIWAR_FROM_EMAIL

    built_subject, built_html, text = build_ow_upgrade_email(
        order, from_lens, to_lens, note, use_ai=use_ai)
    subject = (subject or "").strip() or built_subject
    html = (body_html or "").strip() or built_html

    if dry_run:
        return {
            "dry_run": True, "to": email or "(no email)", "from": from_addr,
            "cc": config.OPTIWAR_ADMIN_CC or "(none)", "subject": subject,
            "html": html, "text": text, "whatsapp_to": phone or "(no phone)",
            "wa_template": "ow_upgrade",
            "wa_body": [name, f"OW-{order_id}", to_lens],
        }

    results = {}
    if "email" in channels:
        if email:
            results["email"] = send_email(
                email, subject, html, text, from_addr=from_addr,
                admin_cc=config.OPTIWAR_ADMIN_CC or None,
                smtp_host=config.OPTIWAR_SMTP_HOST or None,
                smtp_port=config.OPTIWAR_SMTP_PORT or None,
                smtp_user=config.OPTIWAR_SMTP_USER or None,
                smtp_pass=config.OPTIWAR_SMTP_PASS or None)
        else:
            results["email"] = (False, "No customer email on order")
    if "whatsapp" in channels:
        if phone:
            results["whatsapp"] = send_msg91_wa_template(
                phone, "ow_upgrade",
                body_values=[name, f"OW-{order_id}", to_lens],
                lang="en", country_code="IN")
        else:
            results["whatsapp"] = (False, "No customer phone on order")
    if "sms" in channels:
        if phone:
            results["sms"] = send_sms(phone, text)
        else:
            results["sms"] = (False, "No customer phone on order")

    _log_results("ow_upgrade", order_id, results, {"email": email, "whatsapp": phone, "sms": phone})
    return results


def notify_ow_upgrade_withdrawn(order: dict, to_lens: str = "", reason: str = "",
                                channels: list = None):
    """Optional courtesy notice that a previously-offered free lens upgrade has been
    withdrawn and the order reverts to the original complimentary lens. Email only
    has a guaranteed path; WhatsApp needs a dedicated approved template."""
    if channels is None:
        channels = ["email"]
    email = (order.get("email") or "").strip()
    phone = (order.get("telephone") or "").strip()
    order_id = order.get("order_id")
    name = (order.get("firstname", "") or "").strip() or "Customer"
    from_addr = config.OPTIWAR_FROM_EMAIL
    lens_txt = (to_lens or "lens").strip()
    subject = f"Update on your Optiwar order OW-{order_id}"
    html = f"""
    <div style="font-family: Arial, Helvetica, sans-serif; max-width: 640px; margin: 0 auto; color:#1a1a1a;">
        <div style="background:{OW_BRAND};padding:16px 22px;border-radius:8px 8px 0 0;">
            <span style="color:#fff;font-size:22px;font-weight:800;letter-spacing:1px;">OPTIWAR</span>
        </div>
        <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:22px;">
            <p>Hi {_esc(name)},</p>
            <p>We're writing about your order <strong>OW-{_esc(order_id)}</strong>. The complimentary
            upgrade to <strong>{_esc(lens_txt)}</strong> will no longer be applied, and your order
            continues with the originally selected complimentary lens at the same price.</p>
            {f'<p style="color:#555;font-size:13px;">{_esc(reason)}</p>' if reason else ''}
            <p>If you have any questions, just reply to this email — we're happy to help.</p>
            <p style="margin-top:16px;">Thank you for choosing Optiwar!</p>
        </div>
    </div>
    """
    text = (f"Hi {name}, an update on your Optiwar order OW-{order_id}: the complimentary upgrade "
            f"to {lens_txt} will no longer be applied; your order continues with the original "
            f"complimentary lens at the same price."
            + (f" {reason}" if reason else "")
            + " Questions? Just reply. Thank you for choosing Optiwar!")
    results = {}
    if "email" in channels:
        if email:
            results["email"] = send_email(
                email, subject, html, text, from_addr=from_addr,
                admin_cc=config.OPTIWAR_ADMIN_CC or None,
                smtp_host=config.OPTIWAR_SMTP_HOST or None,
                smtp_port=config.OPTIWAR_SMTP_PORT or None,
                smtp_user=config.OPTIWAR_SMTP_USER or None,
                smtp_pass=config.OPTIWAR_SMTP_PASS or None)
        else:
            results["email"] = (False, "No customer email on order")
    if "whatsapp" in channels:
        results["whatsapp"] = (False, "No approved WhatsApp template for upgrade withdrawal")
    _log_results("ow_upgrade_withdrawn", order_id, results, {"email": email, "whatsapp": phone})
    return results


def notify_ow_refund_accounts(order: dict, accounts_email: str, amount: str = "",
                              reason: str = "", channel_results: dict = None,
                              refunded_by: str = "", dry_run: bool = False,
                              method: str = None, status_before: str = None,
                              status_after: str = None):
    """Send a refund summary to the Accounts team (with admin@optiwar.com always CC'd)
    so the refund can be booked. Includes the per-channel EWS delivery receipts as proof.
    channel_results: {channel: (ok, message)} from notify_ow_partial_refund."""
    accounts_email = (accounts_email or "").strip()
    order_id = order.get("order_id")
    cust = (order.get("firstname", "") or "").strip() or "Customer"
    cust_email = (order.get("email") or "").strip() or "—"
    cust_phone = (order.get("telephone") or "").strip() or "—"
    from_addr = config.OPTIWAR_FROM_EMAIL
    amt = (amount or "").strip() or "—"
    why = (reason or "").strip() or "—"
    method = (method or "").strip().upper()
    if status_before and status_after and status_before != status_after:
        status_sentence = (
            f"Order status changed: {status_before} → {status_after}.")
    elif status_after:
        status_sentence = f"Order status unchanged ({status_after})."
    else:
        status_sentence = None
    html_status = (status_sentence or
                   "Order status is unchanged (Shipped).")
    text_status = status_sentence or "Order status unchanged (Shipped)."
    if method == "UPI":
        method_sentence = (
            "Paid to the customer by UPI by Ops staff (details above); "
            "recorded from Ops. No gateway refund was made.")
    elif method == "GATEWAY":
        method_sentence = "Refunded through Razorpay from Ops."
    else:
        method_sentence = (
            "This is a record-only refund raised from the ops dashboard.")
    text_method = (method_sentence
                   if method or status_sentence
                   else "Record-only refund from ops dashboard.")

    # Build a proof table of the EWS that went to the customer.
    label = {"email": "Email", "whatsapp": "WhatsApp", "sms": "SMS"}
    rows = ""
    for ch, res in (channel_results or {}).items():
        ok = bool(res[0]) if isinstance(res, (tuple, list)) and res else False
        msg = str(res[1]) if isinstance(res, (tuple, list)) and len(res) > 1 else ""
        ref = _extract_reference(msg)
        state = ("<span style='color:#16a34a;font-weight:700;'>Sent</span>" if ok
                 else "<span style='color:#b42318;font-weight:700;'>Failed</span>")
        rows += (f"<tr><td style='padding:4px 8px;border:1px solid #eee;'>{label.get(ch, ch)}</td>"
                 f"<td style='padding:4px 8px;border:1px solid #eee;'>{state}</td>"
                 f"<td style='padding:4px 8px;border:1px solid #eee;font-family:monospace;font-size:11px;'>"
                 f"{_esc(ref or '—')}</td></tr>")
    proof = ("<table style='border-collapse:collapse;font-size:12px;margin:8px 0;'>"
             "<tr style='background:#f6f8fa;'><th style='padding:4px 8px;border:1px solid #eee;text-align:left;'>Channel</th>"
             "<th style='padding:4px 8px;border:1px solid #eee;text-align:left;'>Status</th>"
             "<th style='padding:4px 8px;border:1px solid #eee;text-align:left;'>Receipt / Ref</th></tr>"
             f"{rows}</table>") if rows else "<p>No customer notifications were sent.</p>"

    subject = f"Refund to book — OW-{order_id} — {amt}"
    html = (f"<div style='font-family:Segoe UI,Arial,sans-serif;max-width:620px;margin:0 auto;'>"
            f"<div style='background:{OW_ACCENT};color:#fff;padding:14px 18px;border-radius:8px 8px 0 0;'>"
            f"<b>Optiwar</b> — Refund for Accounts</div>"
            f"<div style='border:1px solid #eee;border-top:0;padding:18px;border-radius:0 0 8px 8px;'>"
            f"<p>Please book the following refund:</p>"
            f"<table style='border-collapse:collapse;font-size:13px;'>"
            f"<tr><td style='padding:3px 10px 3px 0;color:#555;'>Order</td><td><b>OW-{_esc(str(order_id))}</b></td></tr>"
            f"<tr><td style='padding:3px 10px 3px 0;color:#555;'>Customer</td><td>{_esc(cust)}</td></tr>"
            f"<tr><td style='padding:3px 10px 3px 0;color:#555;'>Customer email</td><td>{_esc(cust_email)}</td></tr>"
            f"<tr><td style='padding:3px 10px 3px 0;color:#555;'>Customer phone</td><td>{_esc(cust_phone)}</td></tr>"
            f"<tr><td style='padding:3px 10px 3px 0;color:#555;'>Refund amount</td><td><b>{_esc(amt)}</b></td></tr>"
            f"<tr><td style='padding:3px 10px 3px 0;color:#555;'>Reason</td><td>{_esc(why)}</td></tr>"
            f"<tr><td style='padding:3px 10px 3px 0;color:#555;'>Issued by</td><td>{_esc(str(refunded_by) or '—')}</td></tr>"
            f"</table>"
            f"<p style='margin-top:14px;font-weight:600;'>Customer notification receipts (EWS):</p>{proof}"
            f"<p style='color:#94a3b8;font-size:11px;margin-top:14px;'>{_esc(html_status)} "
            f"{_esc(method_sentence)}</p>"
            f"<p style='margin-top:12px;'>— Optiwar Ops</p></div></div>")
    text = (f"Refund to book — OW-{order_id}\n"
            f"Customer: {cust} ({cust_email} / {cust_phone})\n"
            f"Amount: {amt}\nReason: {why}\nIssued by: {refunded_by or '-'}\n"
            f"{text_status} {text_method}")

    if dry_run:
        return {"dry_run": True, "to": accounts_email or "(no accounts email)",
                "from": from_addr, "cc": config.OPTIWAR_ADMIN_CC or "(none)",
                "subject": subject, "html": html, "text": text}

    if not accounts_email:
        return {"accounts": (False, "No accounts email provided")}

    res = send_email(
        accounts_email, subject, html, text, from_addr=from_addr,
        admin_cc=config.OPTIWAR_ADMIN_CC or None,
        smtp_host=config.OPTIWAR_SMTP_HOST or None,
        smtp_port=config.OPTIWAR_SMTP_PORT or None,
        smtp_user=config.OPTIWAR_SMTP_USER or None,
        smtp_pass=config.OPTIWAR_SMTP_PASS or None)
    results = {"accounts": res}
    _log_results("ow_refund_accounts", order_id, results, {"accounts": accounts_email})
    return results


def notify_ow_shipped(order: dict, tracking: str, courier: str = "Delhivery",
                      changes: list = None, note: str = "",
                      dry_run: bool = False, use_ai: bool = True,
                      channels: list = None, eta_days: str = "5-7"):
    """Notify the customer that their Optiwar order shipped (email from admin@optiwar.com,
    WhatsApp via msg91). Includes operator-entered order changes + customer approval.
    When dry_run=True, returns the composed message without sending."""
    if channels is None:
        channels = ["email", "whatsapp"]
    email = (order.get("email") or "").strip()
    phone = (order.get("telephone") or "").strip()
    order_id = order.get("order_id")
    name = (order.get("firstname", "") or "").strip() or "Customer"
    from_addr = config.OPTIWAR_FROM_EMAIL
    track_url = config.courier_track_url(courier, tracking)
    subject, html, text = build_ow_shipped_email(
        order, tracking, courier, changes=changes, note=note, use_ai=use_ai)

    if dry_run:
        return {
            "dry_run": True,
            "would_send": bool(email),
            "to": email or "(no customer email on order)",
            "from": from_addr,
            "cc": config.OPTIWAR_ADMIN_CC or "(none)",
            "subject": subject,
            "html": html,
            "text": text,
            "whatsapp_to": phone or "(no phone on order)",
        }

    results = {}
    if "email" in channels:
        if email:
            results["email"] = send_email(
                email, subject, html, text,
                from_addr=from_addr,
                admin_cc=config.OPTIWAR_ADMIN_CC or None,
                smtp_host=config.OPTIWAR_SMTP_HOST or None,
                smtp_port=config.OPTIWAR_SMTP_PORT or None,
                smtp_user=config.OPTIWAR_SMTP_USER or None,
                smtp_pass=config.OPTIWAR_SMTP_PASS or None,
            )
        else:
            results["email"] = (False, "No customer email on order")

    # WhatsApp via msg91 — order_shipped template.
    # body params: 1=name, 2=order id, 3=est delivery (business days),
    # 4=tracking URL WITHOUT scheme (template prefixes "https://").
    if "whatsapp" in channels and phone:
        track_url_bare = track_url.split("://", 1)[-1]
        results["whatsapp"] = send_msg91_wa_template(
            phone, "order_shipped",
            body_values=[name, f"OW-{order_id}", str(eta_days), track_url_bare],
            lang="en", country_code="IN")

    recipients = {"email": email, "whatsapp": phone}
    _log_results("ow_order_shipped", order_id, results, recipients)
    return results


# ─── New order created by ops (payment pending) ───────────────────────

OW_NEW_ORDER_WA_TEMPLATE = "ow_order_created"


def _ai_new_order_intro(name: str, order_id: str, items: str, total: str,
                        site: str, note: str = "") -> str:
    """DeepSeek-composed opening for the ops-created order email. Falls back to
    fixed copy when the key is absent or the call fails."""
    fallback = (f"<p>Hi {_esc(name)},</p>"
                f"<p>We have created order <strong>OW-{_esc(order_id)}</strong> for you as "
                f"discussed. Please complete the payment using the link below and we will "
                f"start work on your lenses right away.</p>")
    api_key = getattr(config, "DEEPSEEK_API_KEY", "")
    if not api_key:
        return fallback
    try:
        system_prompt = (
            "You write concise, warm order-confirmation copy for Optiwar, an online "
            "eyewear/contact-lens retailer. Output ONLY HTML paragraphs (<p>...</p>), no "
            "headings, no subject, no CSS, no links. 2 short paragraphs max. Address the "
            "customer by first name. Say the order has been created for them by the Optiwar "
            "team, that payment is still pending, and that paying the link confirms it. "
            "Do not invent delivery dates or discounts.")
        user_prompt = (f"Customer first name: {name}\nOrder: OW-{order_id}\n"
                       f"Items: {items}\nAmount payable: {total}\nSite: {site}\n"
                       f"Operator note: {note or '(none)'}\nWrite the copy.")
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat",
                  "messages": [{"role": "system", "content": system_prompt},
                               {"role": "user", "content": user_prompt}],
                  "temperature": 0.5, "max_tokens": 400},
            timeout=30)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        if "<p" not in content:
            content = "<p>" + content.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
        return content
    except Exception as e:
        log.warning("DeepSeek new-order intro failed: %s", e)
        return fallback


def _amount_str(v) -> str:
    """Money as the operator entered it: 200 not 200.0, 199.50 kept."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return str(int(f)) if f == int(f) else f"{f:.2f}"


def _rx_text_lines(items) -> str:
    """Prescription for the plain-text part, one line per eye it has."""
    out = []
    for i in items:
        right = (i.get("right_eye") or "").strip()
        left = (i.get("left_eye") or "").strip()
        if not right and not left:
            continue
        out.append(f"{i.get('name','')} — prescription (SPH/CYL/AXIS/ADD)")
        if right:
            out.append(f"  Right (OD): {right}")
        if left:
            out.append(f"  Left (OS):  {left}")
    return "\n".join(out)


def build_ow_new_order_email(order: dict, use_ai: bool = True):
    """Build (subject, html, text) for an ops-created Optiwar order awaiting payment.

    order keys: order_id, firstname, email, telephone, items (list of dicts with
    name/qty/lens/total), total_display, pay_link, site, note."""
    order_id = order.get("order_id", "")
    name = (order.get("firstname") or "").strip() or "Customer"
    items = order.get("items") or []
    total_display = order.get("total_display", "")
    pay_link = (order.get("pay_link") or "").strip()
    site = order.get("site") or "optiwar.com"
    note = order.get("note") or ""
    items_text = ", ".join(f"{i.get('name','')} x{i.get('qty',1)}" for i in items)

    intro = (_ai_new_order_intro(name, order_id, items_text, total_display, site, note)
             if use_ai else
             f"<p>Hi {_esc(name)},</p><p>We have created order "
             f"<strong>OW-{_esc(order_id)}</strong> for you. Payment is pending.</p>")

    rows = ""
    for i in items:
        lens = i.get("lens") or ""
        lens_row = (f"<div style='font-size:12px;color:#555;'>{_esc(lens)}</div>"
                    if lens else "")
        # The lens is made to this prescription, so the customer has to be able to
        # check it before paying — the same SPH/CYL/AXIS/ADD table the storefront
        # and the confirmation emails show.
        rx_row = i.get("rx_html") or _rx_prescription_table(i)
        addon_row = ""
        addons = [a for a in (i.get("addons") or []) if (a or {}).get("name")]
        if addons:
            addon_row = (
                "<div style='font-size:12px;color:#555;margin-top:6px;'>"
                + "<br>".join(
                    "+ " + _esc(a["name"])
                    + (f" ({_esc(i.get('symbol',''))}{_esc(_amount_str(a.get('price')))})"
                       if a.get("price") else "")
                    for a in addons)
                + "</div>")
        rx_row += addon_row
        rows += (
            "<tr><td style='padding:10px 12px;border-bottom:1px solid #eee;'>"
            f"<div style='font-weight:700;'>{_esc(i.get('name',''))}</div>"
            f"<div style='font-size:12px;color:#777;'>{_esc(i.get('code',''))} "
            f"&middot; Qty {_esc(i.get('qty',1))}</div>{lens_row}{rx_row}</td>"
            "<td style='padding:10px 12px;border-bottom:1px solid #eee;text-align:right;"
            f"white-space:nowrap;font-weight:700;'>{_esc(i.get('total',''))}</td></tr>")

    pay_button = ""
    if pay_link:
        pay_button = (
            f"<p style='margin:20px 0;'><a href='{_esc(pay_link)}' "
            f"style='background:{OW_ACCENT};color:#fff;text-decoration:none;padding:12px 22px;"
            f"border-radius:6px;font-weight:700;display:inline-block;'>Pay {_esc(total_display)}"
            "</a></p>"
            f"<p style='font-size:11px;color:#777;word-break:break-all;'>{_esc(pay_link)}</p>")
    else:
        pay_button = ("<p style='margin:20px 0;font-size:13px;color:#555;'>Our team will send "
                      "you the payment link shortly.</p>")

    note_block = ""
    if note:
        note_block = (f"<p style='font-size:12px;color:#555;'><strong>Note:</strong> "
                      f"{_esc(note)}</p>")

    subject = f"Your Optiwar order OW-{order_id} — payment pending"
    html = f"""
    <div style="font-family: Arial, Helvetica, sans-serif; max-width: 640px; margin: 0 auto; color:#1a1a1a;">
        <div style="background:{OW_BRAND};padding:16px 22px;border-radius:8px 8px 0 0;">
            <span style="color:#fff;font-size:22px;font-weight:800;letter-spacing:1px;">OPTIWAR</span>
            <span style="color:#cfe0f3;font-size:12px;margin-left:8px;">Order Created</span>
        </div>
        <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:22px;">
            <div style="line-height:1.6;">{intro}</div>
            <table style="width:100%;border-collapse:collapse;margin-top:14px;border:1px solid #eee;">
                {rows}
                <tr><td style="padding:10px 12px;font-weight:800;">Amount payable</td>
                    <td style="padding:10px 12px;text-align:right;font-weight:800;">{_esc(total_display)}</td></tr>
            </table>
            {pay_button}
            {note_block}
            <p style="font-size:12px;color:#555;">You can sign in at
               <a href="https://{_esc(site)}/my_orders">{_esc(site)}/my_orders</a>
               with this email or your phone number to view the order.</p>
            <hr style="border:none;border-top:1px solid #eee;margin-top:22px;">
            <p style="color:#94a3b8;font-size:11px;">Automated notification from admin@optiwar.com.</p>
        </div>
    </div>
    """
    rx_text = _rx_text_lines(items)
    text = (f"Hi {name}, we have created your Optiwar order OW-{order_id} "
            f"({items_text}) for {total_display}. Payment is pending"
            + (f" — pay here: {pay_link}" if pay_link else "; we will send the payment link shortly")
            + f". View it at https://{site}/my_orders. Thank you for choosing Optiwar!"
            + (f"\n\n{rx_text}" if rx_text else ""))
    return subject, html, text


def notify_ow_new_order(order: dict, channels: list = None, dry_run: bool = False,
                        subject: str = "", body_html: str = "", use_ai: bool = True):
    """Tell the customer an ops-created order is waiting for payment.

    WhatsApp uses the 'ow_order_created' msg91 template (name, order id, items,
    amount). Until Meta approves that template msg91 returns an error, which is
    logged and reported as a failure — email and SMS still carry the full message."""
    if channels is None:
        channels = []
    email = (order.get("email") or "").strip()
    phone = (order.get("telephone") or "").strip()
    order_id = order.get("order_id")
    name = (order.get("firstname") or "").strip() or "Customer"
    items = order.get("items") or []
    items_summary = ", ".join(f"{i.get('name','')} x{i.get('qty',1)}" for i in items)[:200]
    total_display = order.get("total_display", "")
    from_addr = config.OPTIWAR_FROM_EMAIL

    built_subject, built_html, text = build_ow_new_order_email(order, use_ai=use_ai)
    subject = (subject or "").strip() or built_subject
    html = (body_html or "").strip() or built_html
    site_host = order.get("site") or "optiwar.com"
    wa_body = [name, order_id, items_summary, total_display, site_host]

    if dry_run:
        return {"dry_run": True, "to": email or "(no email)", "from": from_addr,
                "cc": config.OPTIWAR_ADMIN_CC or "(none)", "subject": subject,
                "html": html, "text": text, "whatsapp_to": phone or "(no phone)",
                "wa_template": OW_NEW_ORDER_WA_TEMPLATE, "wa_body": wa_body}

    results = {}
    if "email" in channels:
        if email:
            results["email"] = send_email(
                email, subject, html, text, from_addr=from_addr,
                admin_cc=config.OPTIWAR_ADMIN_CC or None,
                smtp_host=config.OPTIWAR_SMTP_HOST or None,
                smtp_port=config.OPTIWAR_SMTP_PORT or None,
                smtp_user=config.OPTIWAR_SMTP_USER or None,
                smtp_pass=config.OPTIWAR_SMTP_PASS or None)
        else:
            results["email"] = (False, "No customer email on order")
    if "whatsapp" in channels:
        if phone:
            results["whatsapp"] = send_msg91_wa_template(
                phone, OW_NEW_ORDER_WA_TEMPLATE, body_values=wa_body,
                lang="en", country_code="IN")
        else:
            results["whatsapp"] = (False, "No customer phone on order")
    if "sms" in channels:
        if phone:
            results["sms"] = send_sms(phone, text)
        else:
            results["sms"] = (False, "No customer phone on order")

    _log_results("ow_order_created", order_id, results,
                 {"email": email, "whatsapp": phone, "sms": phone})
    return results


# ═══════════════════════════════════════════
# Refund issued through the Optiwar refund API
# ═══════════════════════════════════════════
def _ai_refund_summary(order: dict, amount_text: str, reason_label: str,
                       note: str = "") -> str:
    """DeepSeek-composed opening paragraphs for a refund notification. AI writes
    words only — the amount it is handed has already been settled with the payment
    provider. Any failure falls back to the standard wording, because a refund the
    customer's bank has must be announced whether or not an LLM answers."""
    name = (order.get("firstname", "") or "").strip() or "there"
    fallback = (f"<p>Hi {_esc(name)},</p>"
                f"<p>We've refunded <strong>{_esc(amount_text)}</strong> for your order "
                f"<strong>OW-{_esc(str(order.get('order_id', '')))}</strong>"
                + (f" ({_esc(reason_label)})" if reason_label else "") + ".</p>"
                "<p>The amount goes back to the method you paid with. Banks usually take "
                "5–7 working days to show it.</p>")
    api_key = getattr(config, "DEEPSEEK_API_KEY", "")
    if not api_key:
        return fallback
    try:
        system_prompt = (
            "You write concise, courteous customer-notification copy for Optiwar, an "
            "online eyewear/contact-lens retailer. Output ONLY HTML paragraphs "
            "(<p>...</p>) — no headings, no subject line, no CSS, 3 short paragraphs "
            "max. Address the customer by first name. The message confirms a refund "
            "that has ALREADY been sent to their payment method. State the amount "
            "EXACTLY as given, never recalculate it, never mention any other amount "
            "or currency, and never promise a date. Say bank settlement usually takes "
            "5-7 working days. Apologetic but not grovelling.")
        user_prompt = (
            f"Customer first name: {name}\n"
            f"Order: OW-{order.get('order_id', '')}\n"
            f"Refund amount (use verbatim): {amount_text}\n"
            f"Reason: {reason_label or 'refund requested'}\n"
            f"Operator note: {note or '(none)'}\n"
            "Write the refund confirmation copy.")
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat",
                  "messages": [{"role": "system", "content": system_prompt},
                               {"role": "user", "content": user_prompt}],
                  "temperature": 0.4, "max_tokens": 500},
            timeout=30)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        if "<p" not in content:
            content = "<p>" + content.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
        return content
    except Exception as e:
        log.warning("DeepSeek refund-summary failed: %s", e)
        return fallback


def build_ow_refund_email(order: dict, amount_text: str, reason_label: str = "",
                          note: str = "", partial: bool = False,
                          use_ai: bool = True, upi: dict = None):
    """Build (subject, html, text) for the refund-issued customer email."""
    order_id = order.get("order_id", "")
    name = (order.get("firstname", "") or "").strip() or "Customer"
    if upi:
        intro = _ai_refund_summary_fallback(
            order, amount_text, reason_label, upi=upi)
    else:
        intro = (_ai_refund_summary(order, amount_text, reason_label, note) if use_ai
                 else _ai_refund_summary_fallback(order, amount_text, reason_label))
    kind = "Partial refund" if partial else "Refund"
    note_block = ""
    if note:
        note_block = (f"<p style='font-size:12px;color:#555;margin-top:10px;'>"
                      f"<strong>Note:</strong> {_esc(note)}</p>")
    subject = f"{kind} of {amount_text} for your Optiwar order OW-{order_id}"
    html = f"""
    <div style="font-family: Arial, Helvetica, sans-serif; max-width: 640px; margin: 0 auto; color:#1a1a1a;">
        <div style="background:{OW_BRAND};padding:16px 22px;border-radius:8px 8px 0 0;">
            <span style="color:#fff;font-size:22px;font-weight:800;letter-spacing:1px;">OPTIWAR</span>
            <span style="color:#cfe0f3;font-size:12px;margin-left:8px;">{_esc(kind)}</span>
        </div>
        <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:22px;">
            <div style="line-height:1.6;">{intro}</div>
            <div style="background:#f0fdf4; border:1px solid #bbf7d0; padding:14px 16px; border-radius:8px; margin:16px 0;">
                <div style="font-size:12px;color:#166534;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;">{_esc(kind)} sent</div>
                <div style="font-size:22px;font-weight:800;margin-top:4px;">{_esc(amount_text)}</div>
                <div style="font-size:12px;color:#166534;margin-top:2px;">Order OW-{_esc(str(order_id))}</div>
            </div>
            {note_block}
            <p style="margin-top:16px;">Thank you for your patience.</p>
            <hr style="border:none;border-top:1px solid #eee;margin-top:22px;">
            <p style="color:#94a3b8;font-size:11px;">Automated notification from admin@optiwar.com.</p>
        </div>
    </div>
    """
    if upi:
        text = (f"Hi {name}, we've refunded {amount_text} to your UPI ID "
                f"{upi.get('upi_vpa') or ''} on {upi.get('paid_on') or ''}, UTR "
                f"{upi.get('utr') or ''}, for your Optiwar order OW-{order_id}"
                + (f" ({reason_label})" if reason_label else "")
                + ". Thank you for your patience. — Optiwar")
    else:
        text = (f"Hi {name}, we've refunded {amount_text} for your Optiwar order OW-{order_id}"
                + (f" ({reason_label})" if reason_label else "")
                + ". It goes back to the method you paid with; banks usually take 5-7 working "
                  "days to show it. Thank you for your patience. — Optiwar")
    return subject, html, text


def _ai_refund_summary_fallback(order: dict, amount_text: str, reason_label: str = "",
                                upi: dict = None) -> str:
    """The standard wording, used when AI is switched off or unavailable."""
    name = (order.get("firstname", "") or "").strip() or "there"
    if upi:
        return (f"<p>Hi {_esc(name)},</p>"
                f"<p>We've refunded <strong>{_esc(amount_text)}</strong> to UPI ID "
                f"<strong>{_esc(upi.get('upi_vpa') or '')}</strong> on "
                f"<strong>{_esc(upi.get('paid_on') or '')}</strong>, UTR "
                f"<strong>{_esc(upi.get('utr') or '')}</strong>, for your order "
                f"<strong>OW-{_esc(str(order.get('order_id', '')))}</strong>"
                + (f" ({_esc(reason_label)})" if reason_label else "") + ".</p>")
    return (f"<p>Hi {_esc(name)},</p>"
            f"<p>We've refunded <strong>{_esc(amount_text)}</strong> for your order "
            f"<strong>OW-{_esc(str(order.get('order_id', '')))}</strong>"
            + (f" ({_esc(reason_label)})" if reason_label else "") + ".</p>"
            "<p>The amount goes back to the method you paid with. Banks usually take "
            "5–7 working days to show it.</p>")


def notify_ow_refund_issued(order: dict, amount_text: str, reason_label: str = "",
                            note: str = "", channels: list = None,
                            dry_run: bool = False, subject: str = "",
                            body_html: str = "", partial: bool = False,
                            use_ai: bool = True, upi: dict = None):
    """Tell the customer about a refund the payment provider has already accepted.
    `subject`/`body_html` are the operator-approved text and win over anything
    composed here. A channel failing is logged and returned — it never unwinds a
    refund that has left the account."""
    if channels is None:
        channels = []
    email = (order.get("email") or "").strip()
    phone = (order.get("telephone") or "").strip()
    order_id = order.get("order_id")
    name = (order.get("firstname", "") or "").strip() or "Customer"
    from_addr = config.OPTIWAR_FROM_EMAIL

    built_subject, built_html, text = build_ow_refund_email(
        order, amount_text, reason_label, note, partial=partial, use_ai=use_ai,
        upi=upi)
    subject = (subject or "").strip() or built_subject
    html = (body_html or "").strip() or built_html
    wa_body = [name, amount_text, f"OW-{order_id}", reason_label or "-"]

    if dry_run:
        return {
            "dry_run": True, "to": email or "(no email)", "from": from_addr,
            "cc": config.OPTIWAR_ADMIN_CC or "(none)", "subject": subject,
            "html": html, "text": text, "whatsapp_to": phone or "(no phone)",
            "wa_template": "ow_refunds", "wa_body": wa_body,
        }

    results = {}
    if "email" in channels:
        if email:
            results["email"] = send_email(
                email, subject, html, text, from_addr=from_addr,
                admin_cc=config.OPTIWAR_ADMIN_CC or None,
                smtp_host=config.OPTIWAR_SMTP_HOST or None,
                smtp_port=config.OPTIWAR_SMTP_PORT or None,
                smtp_user=config.OPTIWAR_SMTP_USER or None,
                smtp_pass=config.OPTIWAR_SMTP_PASS or None)
        else:
            results["email"] = (False, "No customer email on order")
    if "whatsapp" in channels:
        if phone:
            results["whatsapp"] = send_msg91_wa_template(
                phone, "ow_refunds", body_values=wa_body,
                lang="en", country_code="IN")
        else:
            results["whatsapp"] = (False, "No customer phone on order")
    if "sms" in channels:
        if phone:
            results["sms"] = send_sms(phone, text)
        else:
            results["sms"] = (False, "No customer phone on order")

    _log_results("ow_refund_issued", order_id, results,
                 {"email": email, "whatsapp": phone, "sms": phone})
    return results
