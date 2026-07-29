"""EWS Notification Module — Email, WhatsApp, SMS for payment alerts.

Sends notifications via 3 channels simultaneously:
- Email: via Flask-Mail (already configured)
- WhatsApp: via MSG91 WhatsApp Business API
- SMS: via MSG91 Flow API (requires DLT registration)

All attempts are logged with EWS: prefix.
"""

import os
import re
import socket
from datetime import datetime, timezone

import requests as http_requests
from urllib3.util import connection as _urllib3_connection
from flask import current_app
from flask_mail import Message

# MSG91 IP-Security whitelists only this server's IPv4 address; DNS for MSG91/other
# hosts returns IPv6 first, so requests would egress over the (non-whitelisted,
# dynamic) IPv6 and get 401. Force urllib3/requests to use IPv4 so outbound API
# calls originate from the whitelisted IPv4. IPv4 reachability verified for MSG91
# and KET. Set once at import (thread-safe); does not affect smtplib or httpx.
_urllib3_connection.allowed_gai_family = lambda: socket.AF_INET

KET_EVENTS_URL = os.environ.get(
    "KET_EVENTS_URL", "https://support.ket.ltd/new/api/v1/external/events"
)


def _log(msg):
    """Log EWS activity."""
    try:
        current_app.logger.info(f"EWS: {msg}")
    except Exception:
        print(f"EWS: {msg}")


def _to_e164(phone, default_cc='91'):
    """Best-effort normalise a phone to E.164 (+<cc><number>). Returns None if unusable."""
    if not phone:
        return None
    raw = str(phone).strip()
    has_plus = raw.startswith('+')
    digits = re.sub(r'\D', '', raw)
    if not digits:
        return None
    if has_plus:
        return '+' + digits
    digits = digits.lstrip('0')
    if len(digits) == 10:
        return '+' + default_cc + digits
    return '+' + digits


def _ket_event_key(site_host):
    """Return the per-site KET API key based on the originating host."""
    host = (site_host or '').lower()
    if 'optiwar.in' in host or 'in.optiwar' in host:
        return os.environ.get('KET_SUPPORT_KEY_INOPTIWAR', '')
    return os.environ.get('KET_SUPPORT_KEY_OPTIWAR', '')


def _emit_ket_event(event_type, event_id, customer, data, site_host, channels=None):
    """Fire-and-forget emit to the KET comms hub (/external/events). Never raises.

    Idempotent on event_id (KET never double-sends the same event_id). Email for
    payment/order stays app-side during overlap, so the default channel allow-list
    is WhatsApp+SMS (KET delivers those once its channels are live).
    """
    try:
        if not current_app.config.get('KET_EVENTS_ENABLED', True):
            _log(f"KET_EVENT:DISABLED type={event_type} id={event_id}")
            return None
        if not (customer.get('email') or customer.get('phone')):
            _log(f"KET_EVENT:SKIPPED type={event_type} id={event_id} reason=no_recipient")
            return None
        api_key = _ket_event_key(site_host)
        if not api_key:
            _log(f"KET_EVENT:SKIPPED type={event_type} id={event_id} reason=no_api_key")
            return None
        if channels is None:
            channels = [
                c.strip()
                for c in current_app.config.get('KET_EVENT_CHANNELS', 'whatsapp,sms').split(',')
                if c.strip()
            ]
        payload = {
            "event_type": event_type,
            "event_id": event_id,
            "occurred_at": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            "customer": {k: v for k, v in customer.items() if v},
            "data": data or {},
        }
        if channels:
            payload["channels"] = channels
        resp = http_requests.post(
            KET_EVENTS_URL,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            _log(f"KET_EVENT:SENT type={event_type} id={event_id} resp={resp.text[:300]}")
            return resp
        _log(f"KET_EVENT:FAILED type={event_type} id={event_id} status={resp.status_code} body={resp.text[:200]}")
    except Exception as e:
        _log(f"KET_EVENT:ERROR type={event_type} id={event_id} error={e}")
    return None


def send_email(to_email, subject, body_html, body_text=None, cc_emails=None):
    """Send email notification via Flask-Mail."""
    try:
        from flaskr import mail
        admin_email = current_app.config.get('MAIL_DEFAULT_SENDER', 'admin@optiwar.com')
        # Build CC list (profile email copy), deduplicate against to_email and admin
        cc_list = []
        if cc_emails:
            for e in cc_emails:
                if e and e != to_email and e != admin_email:
                    cc_list.append(e)
        msg = Message(
            subject=subject,
            recipients=[to_email],
            cc=cc_list if cc_list else [],
            bcc=[admin_email] if to_email != admin_email else [],
            html=body_html,
            body=body_text or subject,
            sender=admin_email
        )
        mail.send(msg)
        _log(f"EMAIL:SUCCESS to={to_email} cc={cc_list} subject={subject}")
        return True
    except Exception as e:
        _log(f"EMAIL:FAILED to={to_email} subject={subject} error={e}")
        return False


def send_whatsapp(to_phone, template_name, components=None):
    """Send WhatsApp message via MSG91 API."""
    auth_key = current_app.config.get('MSG91_AUTH_KEY', '')
    wa_number = current_app.config.get('MSG91_WHATSAPP_NUMBER', '')

    if not auth_key:
        _log(f"WHATSAPP:SKIPPED to={to_phone} template={template_name} reason=no_auth_key")
        return False

    if not wa_number:
        _log(f"WHATSAPP:SKIPPED to={to_phone} template={template_name} reason=no_wa_number")
        return False

    try:
        url = "https://control.msg91.com/api/v5/whatsapp/whatsapp-outbound-message/bulk/"
        headers = {
            'Content-Type': 'application/json',
            'authkey': auth_key
        }
        payload = {
            "integrated_number": wa_number,
            "content_type": "template",
            "payload": {
                "messaging_product": "whatsapp",
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {
                        "code": "en",
                        "policy": "deterministic"
                    },
                    "to_and_components": [
                        {
                            "to": [to_phone],
                            "components": components or {}
                        }
                    ]
                }
            }
        }
        resp = http_requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code in (200, 201):
            _log(f"WHATSAPP:SUCCESS to={to_phone} template={template_name} status={resp.status_code}")
            return True
        else:
            _log(f"WHATSAPP:FAILED to={to_phone} template={template_name} status={resp.status_code} body={resp.text[:200]}")
            return False
    except Exception as e:
        _log(f"WHATSAPP:FAILED to={to_phone} template={template_name} error={e}")
        return False


def send_sms(to_phone, flow_id, variables=None):
    """Send SMS via MSG91 Flow API."""
    auth_key = current_app.config.get('MSG91_AUTH_KEY', '')
    sender = current_app.config.get('MSG91_SMS_SENDER', '')

    if not auth_key:
        _log(f"SMS:SKIPPED to={to_phone} flow={flow_id} reason=no_auth_key")
        return False

    if not flow_id:
        _log(f"SMS:SKIPPED to={to_phone} reason=no_flow_id")
        return False

    try:
        url = "https://control.msg91.com/api/v5/flow/"
        headers = {
            'Content-Type': 'application/json',
            'authkey': auth_key
        }
        recipient = {"mobiles": to_phone}
        if variables:
            recipient.update(variables)

        payload = {
            "flow_id": flow_id,
            "sender": sender,
            "recipients": [recipient]
        }
        resp = http_requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code in (200, 201):
            _log(f"SMS:SUCCESS to={to_phone} flow={flow_id} status={resp.status_code}")
            return True
        else:
            _log(f"SMS:FAILED to={to_phone} flow={flow_id} status={resp.status_code} body={resp.text[:200]}")
            return False
    except Exception as e:
        _log(f"SMS:FAILED to={to_phone} flow={flow_id} error={e}")
        return False


def notify_payment_attempted(customer_email, customer_phone, order_id, amount, currency_symbol, site_host, profile_email=None):
    """Trigger EWS for payment attempted."""
    _log(f"TRIGGER:PAYMENT_ATTEMPTED order={order_id} email={customer_email} profile_email={profile_email} phone={customer_phone} amount={currency_symbol}{amount} host={site_host}")

    results = {'email': False, 'whatsapp': False, 'sms': False}

    # payment_attempted is an internal notice only: email admin, no customer email, no WhatsApp/SMS.
    admin_email = current_app.config.get('ADMIN_NOTIFY_EMAIL', 'admin@optiwar.com')
    subject = f"[Admin] Payment Initiated \u2014 Order {order_id}"
    body_html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
        <h2 style="color:#4f46e5;">Payment Initiated (admin notice)</h2>
        <p>Customer <strong>{customer_email or 'unknown'}</strong> initiated a payment of <strong>{currency_symbol}{amount}</strong> for order <strong>{order_id}</strong> on {site_host}.</p>
        <p style="color:#64748b;font-size:13px;margin-top:30px;">&mdash; Optiwar Team</p>
    </div>
    """
    results['email'] = send_email(admin_email, subject, body_html)

    _log(f"RESULT:PAYMENT_ATTEMPTED order={order_id} admin_only admin={admin_email} email={results['email']} whatsapp={results['whatsapp']} sms={results['sms']}")
    return results


def notify_payment_success(customer_email, customer_phone, order_id, amount, currency_symbol, site_host, gateway='', profile_email=None):
    """Trigger EWS for payment success."""
    _log(f"TRIGGER:PAYMENT_SUCCESS order={order_id} email={customer_email} profile_email={profile_email} phone={customer_phone} amount={currency_symbol}{amount} gateway={gateway} host={site_host}")

    results = {'email': False, 'whatsapp': False, 'sms': False}

    subject = f"Payment Successful \u2014 Order {order_id} Confirmed!"
    body_html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
        <h2 style="color:#16a34a;">Payment Successful!</h2>
        <p>Your payment of <strong>{currency_symbol}{amount}</strong> for order <strong>{order_id}</strong> has been confirmed.</p>
        <p>Thank you for shopping with Optiwar! Your order is now being processed.</p>
        <p style="margin-top:20px;"><a href="https://{site_host}/success/{order_id}" style="background:#4f46e5;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">View Order</a></p>
        <p style="color:#64748b;font-size:13px;margin-top:30px;">&mdash; Optiwar Team</p>
    </div>
    """
    results['email'] = send_email(customer_email, subject, body_html, cc_emails=[profile_email])

    if customer_phone:
        phone = customer_phone.replace('+', '').replace(' ', '').replace('-', '')
        components = {
            "body_1": {"type": "text", "value": str(order_id)},
            "body_2": {"type": "text", "value": f"{currency_symbol}{amount}"}
        }
        results['whatsapp'] = send_whatsapp(phone, "payment_success_1", components)

    _emit_ket_event(
        'payment_success',
        f'optiwar-pay-{order_id}',
        {'email': customer_email, 'phone': _to_e164(customer_phone)},
        {'order_id': str(order_id), 'amount': amount, 'currency': currency_symbol},
        site_host,
    )

    _log(f"RESULT:PAYMENT_SUCCESS order={order_id} email={results['email']} whatsapp={results['whatsapp']} sms={results['sms']}")
    return results


def notify_payment_failed(customer_email, customer_phone, order_id, amount, currency_symbol, site_host, reason='', profile_email=None):
    """Trigger EWS for payment failure."""
    _log(f"TRIGGER:PAYMENT_FAILED order={order_id} email={customer_email} profile_email={profile_email} phone={customer_phone} amount={currency_symbol}{amount} reason={reason} host={site_host}")

    results = {'email': False, 'whatsapp': False, 'sms': False}

    subject = f"Payment Failed \u2014 Order {order_id}"
    body_html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
        <h2 style="color:#dc2626;">Payment Failed</h2>
        <p>Your payment of <strong>{currency_symbol}{amount}</strong> for order <strong>{order_id}</strong> could not be processed.</p>
        {'<p>Reason: ' + reason + '</p>' if reason else ''}
        <p>Please try again or use a different payment method.</p>
        <p style="margin-top:20px;"><a href="https://{site_host}/checkout" style="background:#4f46e5;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">Retry Payment</a></p>
        <p style="color:#64748b;font-size:13px;margin-top:30px;">&mdash; Optiwar Team</p>
    </div>
    """
    results['email'] = send_email(customer_email, subject, body_html, cc_emails=[profile_email])

    if customer_phone:
        phone = customer_phone.replace('+', '').replace(' ', '').replace('-', '')
        components = {
            "body_1": {"type": "text", "value": str(order_id)},
            "body_2": {"type": "text", "value": f"{currency_symbol}{amount}"}
        }
        results['whatsapp'] = send_whatsapp(phone, "payment_failed", components)

    _log(f"RESULT:PAYMENT_FAILED order={order_id} email={results['email']} whatsapp={results['whatsapp']} sms={results['sms']}")
    return results


# ═══════════════════════════════════════════════════════════════════
# Order Notifications
# ═══════════════════════════════════════════════════════════════════

def notify_order_confirmed(customer_email, customer_phone, customer_name, order_id, amount, currency_symbol, site_host, profile_email=None):
    """Trigger EWS for order confirmed (after successful payment)."""
    _log(f"TRIGGER:ORDER_CONFIRMED order={order_id} email={customer_email} phone={customer_phone} amount={currency_symbol}{amount}")

    results = {'email': False, 'whatsapp': False, 'sms': False}

    subject = f"Order Confirmed — {order_id} | Optiwar"
    body_html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
        <h2 style="color:#16a34a;">Order Confirmed! 🎉</h2>
        <p>Hi {customer_name or 'there'},</p>
        <p>Your order <strong>{order_id}</strong> for <strong>{currency_symbol}{amount}</strong> has been confirmed!</p>
        <p>We're preparing your eyewear now. You'll receive a shipping notification once your order is on its way.</p>
        <p style="margin-top:20px;"><a href="https://{site_host}/success/{order_id}" style="background:#4f46e5;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">View Order</a></p>
        <p style="color:#64748b;font-size:13px;margin-top:30px;">&mdash; Optiwar Team</p>
    </div>
    """
    results['email'] = send_email(customer_email, subject, body_html, cc_emails=[profile_email])

    if customer_phone:
        phone = customer_phone.replace('+', '').replace(' ', '').replace('-', '')
        components = {
            "body_1": {"type": "text", "value": customer_name or "Customer"},
            "body_2": {"type": "text", "value": str(order_id)},
            "body_3": {"type": "text", "value": f"{currency_symbol}{amount}"},
            "body_4": {"type": "text", "value": site_host}
        }
        results['whatsapp'] = send_whatsapp(phone, "order_confirmed", components)

        sms_flow = current_app.config.get('MSG91_ORDER_CONFIRMED_SMS_FLOW', '')
        if sms_flow:
            results['sms'] = send_sms(phone, sms_flow, variables={'order_id': str(order_id), 'amount': f"{currency_symbol}{amount}"})

    _emit_ket_event(
        'order_confirmed',
        f'optiwar-ordconf-{order_id}',
        {'name': customer_name, 'email': customer_email, 'phone': _to_e164(customer_phone)},
        {'order_id': str(order_id), 'amount': amount, 'currency': currency_symbol},
        site_host,
    )

    _log(f"RESULT:ORDER_CONFIRMED order={order_id} email={results['email']} whatsapp={results['whatsapp']} sms={results['sms']}")
    return results


def notify_order_shipped(customer_email, customer_phone, customer_name, order_id, site_host, tracking_info='', delivery_days='5-7', profile_email=None):
    """Trigger EWS for order shipped."""
    _log(f"TRIGGER:ORDER_SHIPPED order={order_id} email={customer_email} phone={customer_phone} tracking={tracking_info}")

    results = {'email': False, 'whatsapp': False, 'sms': False}

    tracking_html = f"<p><strong>Tracking:</strong> {tracking_info}</p>" if tracking_info else ""
    subject = f"Order Shipped — {order_id} is on its way! 🚚"
    body_html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
        <h2 style="color:#2563eb;">Your Order Has Shipped! 🚚</h2>
        <p>Hi {customer_name or 'there'},</p>
        <p>Great news! Your order <strong>{order_id}</strong> has been shipped and is on its way to you.</p>
        {tracking_html}
        <p>Estimated delivery: <strong>{delivery_days} business days</strong></p>
        <p style="margin-top:20px;"><a href="https://{site_host}/success/{order_id}" style="background:#4f46e5;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">Track Order</a></p>
        <p style="color:#64748b;font-size:13px;margin-top:30px;">&mdash; Optiwar Team</p>
    </div>
    """
    results['email'] = send_email(customer_email, subject, body_html, cc_emails=[profile_email])

    if customer_phone:
        phone = customer_phone.replace('+', '').replace(' ', '').replace('-', '')
        components = {
            "body_1": {"type": "text", "value": customer_name or "Customer"},
            "body_2": {"type": "text", "value": str(order_id)},
            "body_3": {"type": "text", "value": str(delivery_days)},
            "body_4": {"type": "text", "value": site_host}
        }
        results['whatsapp'] = send_whatsapp(phone, "order_shipped", components)

        sms_flow = current_app.config.get('MSG91_ORDER_SHIPPED_SMS_FLOW', '')
        if sms_flow:
            results['sms'] = send_sms(phone, sms_flow, variables={'order_id': str(order_id)})

    _emit_ket_event(
        'order_shipped',
        f'optiwar-ordship-{order_id}',
        {'name': customer_name, 'email': customer_email, 'phone': _to_e164(customer_phone)},
        {'order_id': str(order_id), 'tracking_url': tracking_info},
        site_host,
    )

    _log(f"RESULT:ORDER_SHIPPED order={order_id} email={results['email']} whatsapp={results['whatsapp']} sms={results['sms']}")
    return results


# ═══════════════════════════════════════════════════════════════════
# Support Ticket Notifications
# ═══════════════════════════════════════════════════════════════════

def notify_support_ticket_created(customer_email, customer_phone, customer_name, ticket_id, subject_text, site_host, profile_email=None):
    """Trigger EWS when a support ticket is created (from chat, timeout, or manual)."""
    _log(f"TRIGGER:TICKET_CREATED ticket={ticket_id} email={customer_email} phone={customer_phone} subject={subject_text}")

    results = {'email': False, 'whatsapp': False, 'sms': False}

    subject = f"Support Request Received — Ticket #{ticket_id}"
    body_html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
        <h2 style="color:#4f46e5;">We've Received Your Request 📋</h2>
        <p>Hi {customer_name or 'there'},</p>
        <p>Your support request has been created:</p>
        <div style="background:#f8fafc;border-left:4px solid #4f46e5;padding:12px 16px;margin:16px 0;">
            <strong>Ticket #{ticket_id}</strong><br/>
            {subject_text}
        </div>
        <p>Our team will review and respond within <strong>24 hours</strong>.</p>
        <p style="color:#64748b;font-size:13px;margin-top:30px;">&mdash; Optiwar Support</p>
    </div>
    """
    # Support email is owned by KET (sole sender); Optiwar sends only WhatsApp for support.
    if current_app.config.get('SUPPORT_TICKET_EMAIL_ENABLED', False):
        results['email'] = send_email(customer_email, subject, body_html, cc_emails=[profile_email])
    else:
        _log(f"TICKET_EMAIL:SKIPPED ticket={ticket_id} reason=ket_is_sole_support_email_sender")

    if customer_phone:
        phone = customer_phone.replace('+', '').replace(' ', '').replace('-', '')
        components = {
            "body_1": {"type": "text", "value": customer_name or "Customer"},
            "body_2": {"type": "text", "value": str(ticket_id)},
            "body_3": {"type": "text", "value": subject_text[:100]}
        }
        results['whatsapp'] = send_whatsapp(phone, "support_ticket_created", components)

        sms_flow = current_app.config.get('MSG91_TICKET_CREATED_SMS_FLOW', '')
        if sms_flow:
            results['sms'] = send_sms(phone, sms_flow, variables={'ticket_id': str(ticket_id)})

    _log(f"RESULT:TICKET_CREATED ticket={ticket_id} email={results['email']} whatsapp={results['whatsapp']} sms={results['sms']}")
    return results


def notify_support_ticket_resolved(customer_email, customer_phone, customer_name, ticket_id, site_host, profile_email=None):
    """Trigger EWS when a support ticket is resolved."""
    _log(f"TRIGGER:TICKET_RESOLVED ticket={ticket_id} email={customer_email} phone={customer_phone}")

    results = {'email': False, 'whatsapp': False, 'sms': False}

    subject = f"Ticket #{ticket_id} Resolved ✅"
    body_html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
        <h2 style="color:#16a34a;">Your Ticket Has Been Resolved ✅</h2>
        <p>Hi {customer_name or 'there'},</p>
        <p>Your support ticket <strong>#{ticket_id}</strong> has been resolved.</p>
        <p>If you need further assistance, feel free to start a new chat or contact us anytime.</p>
        <p style="margin-top:20px;"><a href="https://{site_host}/search" style="background:#4f46e5;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">Chat With Us</a></p>
        <p style="color:#64748b;font-size:13px;margin-top:30px;">&mdash; Optiwar Support</p>
    </div>
    """
    # Support email is owned by KET (sole sender); Optiwar sends only WhatsApp for support.
    if current_app.config.get('SUPPORT_TICKET_EMAIL_ENABLED', False):
        results['email'] = send_email(customer_email, subject, body_html, cc_emails=[profile_email])
    else:
        _log(f"TICKET_EMAIL:SKIPPED ticket={ticket_id} reason=ket_is_sole_support_email_sender")

    if customer_phone:
        phone = customer_phone.replace('+', '').replace(' ', '').replace('-', '')
        components = {
            "body_1": {"type": "text", "value": customer_name or "Customer"},
            "body_2": {"type": "text", "value": str(ticket_id)},
            "body_3": {"type": "text", "value": site_host}
        }
        results['whatsapp'] = send_whatsapp(phone, "support_ticket_resolved", components)

        sms_flow = current_app.config.get('MSG91_TICKET_RESOLVED_SMS_FLOW', '')
        if sms_flow:
            results['sms'] = send_sms(phone, sms_flow, variables={'ticket_id': str(ticket_id)})

    _log(f"RESULT:TICKET_RESOLVED ticket={ticket_id} email={results['email']} whatsapp={results['whatsapp']} sms={results['sms']}")
    return results
