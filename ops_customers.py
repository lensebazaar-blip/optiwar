"""Ops-only customer creation and first-party face-scan requests."""

import html
import os
import re
import secrets
import string
from datetime import datetime, timedelta

from flask import current_app
from werkzeug.security import generate_password_hash

from . import face_profiles as fp
from . import face_scan_invites as fsi


TEMP_PASSWORD_DAYS = 7
TEMP_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
OTP_GENERIC_MESSAGE = "If this number matches an account, we've sent a code. Enter it below."
WELCOME_SUBJECT = "Welcome to Optiwar \u2013 your login details"
WELCOME_LOGIN_URL = "https://optiwar.com/auth/login"

TEMP_PASSWORDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS customer_temp_passwords (
    customer_id INT NOT NULL,
    expires_at DATETIME NOT NULL,
    created_by VARCHAR(120) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (customer_id),
    KEY idx_customer_temp_passwords_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

LOGIN_OTPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS customer_login_otps (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    customer_id INT NOT NULL,
    phone VARCHAR(24) NOT NULL,
    code_hash CHAR(64) NOT NULL,
    expires_at DATETIME NOT NULL,
    attempts TINYINT UNSIGNED NOT NULL DEFAULT 0,
    used_at DATETIME NULL,
    created_ip VARCHAR(45) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_customer_login_otps_phone_created (phone, created_at),
    KEY idx_customer_login_otps_ip_created (created_ip, created_at),
    KEY idx_customer_login_otps_customer (customer_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (
    ("customer_temp_passwords", TEMP_PASSWORDS_SCHEMA),
    ("customer_login_otps", LOGIN_OTPS_SCHEMA),
)

_SCHEMA_READY = False
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


class CustomerError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class CustomerExists(CustomerError):
    def __init__(self, matches):
        super().__init__("exists", "A customer already matches this email or phone", 409)
        self.matches = matches


def ensure_schema(db):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    cur = db.cursor()
    for _name, ddl in TABLES:
        cur.execute(ddl)
    db.commit()
    _SCHEMA_READY = True


def normalize_email(value):
    email = str(value or "").strip().lower()
    if len(email) > 191 or not _EMAIL_RE.fullmatch(email):
        raise CustomerError("bad_email", "Enter a valid email address")
    return email


def phone_digits(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def normalize_phone(value):
    digits = phone_digits(value)
    if len(digits) < 10 or len(digits) > 15:
        raise CustomerError("bad_phone", "Enter a phone number with at least 10 digits")
    if len(digits) == 10:
        return "+91" + digits
    return "+" + digits


def phone_key(value):
    digits = phone_digits(value)
    return digits[-10:] if len(digits) >= 10 else ""


def _customer_view(row):
    return {
        "customer_id": int(row["customer_id"]),
        "name": row.get("customer_name") or "",
        "email": row.get("customer_email") or "",
        "phone": row.get("customer_phone") or "",
        "date_created": fp._jsonable(row.get("date_created")),
    }


def find_customers(db, email=None, phone=None):
    conditions = []
    params = []
    normalized_email = str(email or "").strip().lower()
    key = phone_key(phone)
    if normalized_email:
        conditions.append("LOWER(customer_email)=%s")
        params.append(normalized_email)
    if key:
        conditions.append(
            "RIGHT(REGEXP_REPLACE(COALESCE(customer_phone, ''), '[^0-9]', ''), 10)=%s")
        params.append(key)
    if not conditions:
        return []
    cur = db.cursor()
    cur.execute(
        "SELECT customer_id, customer_name, customer_email, customer_phone, date_created "
        "FROM customers WHERE %s ORDER BY customer_id DESC" % " OR ".join(conditions),
        tuple(params))
    return [_customer_view(row) for row in cur.fetchall()]


def customer_by_id(db, customer_id):
    cur = db.cursor()
    cur.execute("SELECT customer_id, customer_name, customer_email, customer_phone, "
                "date_created FROM customers WHERE customer_id=%s",
                (int(customer_id),))
    row = cur.fetchone()
    return _customer_view(row) if row else None


def _mask_phone(value):
    digits = phone_digits(value)
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) >= 12 and digits.startswith("91"):
        return "+91 %s\u2022\u2022\u2022\u2022\u2022%s" % (digits[2:4], digits[-3:])
    if len(digits) >= 10:
        return "+%s %s\u2022\u2022\u2022\u2022\u2022%s" % (
            digits[:-10], digits[-10:-8], digits[-3:])
    return "\u2022\u2022\u2022\u2022"


def _welcome_text(name, email, phone, password, otp_enabled):
    lines = [
        "Hello %s," % name,
        "Your Optiwar account has been created by Optiwar Support.",
        "Email: %s" % email,
        "Temporary password: %s" % password,
        "This password works for 7 days. You will be asked to set your own password when you first log in.",
    ]
    if otp_enabled:
        lines.append("You can also log in with a one-time code on WhatsApp (%s)." %
                     _mask_phone(phone))
    lines.extend([
        "[Log in to Optiwar]  -> %s" % WELCOME_LOGIN_URL,
        "If you did not ask for this account, ignore this email or reply to support@optiwar.com.",
        "Optiwar will never ask for card details, OTPs or payments by email.",
        "Regards, Optiwar Support, Factory Outlet Opticals",
    ])
    return "\n".join(lines)


def _welcome_html(name, email, phone, password, otp_enabled):
    text = _welcome_text(name, email, phone, password, otp_enabled)
    paragraphs = "<br>".join(html.escape(line) for line in text.splitlines())
    button = ('<p style="margin:24px 0"><a href="%s" style="background:#5b3df5;'
              'color:#fff;text-decoration:none;padding:12px 22px;border-radius:8px;'
              'display:inline-block">Log in to Optiwar</a></p>' % WELCOME_LOGIN_URL)
    paragraphs = paragraphs.replace(
        html.escape("[Log in to Optiwar]  -> %s" % WELCOME_LOGIN_URL), button)
    return ('<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;'
            'margin:auto;color:#222"><p style="font-size:18px;font-weight:bold;'
            'letter-spacing:.5px">OPTIWAR</p><p>%s</p></div>' % paragraphs)


def welcome_preview(name, email, phone, otp_enabled):
    password = "\u2022" * 10 + " (generated when you press Send)"
    return {
        "subject": WELCOME_SUBJECT,
        "text": _welcome_text(name, email, phone, password, otp_enabled),
    }


def face_scan_preview():
    link = "https://optiwar.com/f/\u2026"
    return {
        "subject": fsi.EMAIL_SUBJECT.format(sender="Optiwar Support"),
        "text": fsi.EMAIL_TEXT.format(sender="Optiwar Support", link=link),
        "whatsapp_text": (fsi.WA_TEMPLATE_BODY.replace("{{1}}", "Optiwar Support")
                          .replace("{{2}}", link)),
    }


def message_preview(kind, name="", email="", phone="", otp_enabled=False):
    if kind == "welcome":
        return welcome_preview(name, email, phone, otp_enabled)
    if kind == "face_scan":
        return face_scan_preview()
    raise CustomerError("bad_kind", "Choose welcome or face_scan")


def _send_welcome_email(to_email, name, phone, password, otp_enabled,
                        mailer=None):
    text = _welcome_text(name, to_email, phone, password, otp_enabled)
    body_html = _welcome_html(name, to_email, phone, password, otp_enabled)
    try:
        if mailer:
            mailer(to_email, WELCOME_SUBJECT, body_html, text)
        else:
            from flask_mail import Message
            message = Message(
                subject=WELCOME_SUBJECT,
                recipients=[to_email],
                html=body_html,
                body=text,
                sender="Optiwar Support <support@optiwar.com>",
                reply_to="support@optiwar.com",
            )
            current_app.extensions["mail"].send(message)
        return {"ok": True, "error": ""}
    except Exception as exc:  # delivery failure does not undo account creation
        current_app.logger.warning("OPS_CUSTOMER:WELCOME_EMAIL_FAILED error=%s",
                                   type(exc).__name__)
        return {"ok": False, "error": type(exc).__name__}


def create_customer(db, name, email, phone, created_by=None, mailer=None,
                    otp_enabled=None, now=None):
    clean_name = " ".join(str(name or "").split())
    if not 2 <= len(clean_name) <= 120:
        raise CustomerError("bad_name", "Name must be between 2 and 120 characters")
    clean_email = normalize_email(email)
    clean_phone = normalize_phone(phone)
    matches = find_customers(db, clean_email, clean_phone)
    if matches:
        raise CustomerExists(matches)

    ensure_schema(db)
    fp.ensure_schema(db)
    password = "".join(secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(10))
    expires_at = (now or datetime.now()) + timedelta(days=TEMP_PASSWORD_DAYS)
    cur = db.cursor()
    try:
        if hasattr(db, "begin"):
            db.begin()
        cur.execute(
            "INSERT INTO customers (customer_name, customer_email, customer_password, "
            "customer_phone) VALUES (%s,%s,%s,%s)",
            (clean_name, clean_email, generate_password_hash(password), clean_phone))
        customer_id = int(cur.lastrowid)
        cur.execute(
            "INSERT INTO customer_temp_passwords "
            "(customer_id, expires_at, created_by) VALUES (%s,%s,%s)",
            (customer_id, expires_at, str(created_by or "")[:120] or None))
        fp.ensure_self(db, customer_id, clean_name)
        db.commit()
    except Exception:
        db.rollback()
        raise

    if otp_enabled is None:
        otp_enabled = bool(current_app.config.get("MSG91_LOGIN_OTP_WA_TEMPLATE"))
    welcome_email = _send_welcome_email(
        clean_email, clean_name, clean_phone, password, bool(otp_enabled), mailer)
    return {"customer_id": customer_id, "welcome_email": welcome_email}


def _channels(value):
    if not isinstance(value, (list, tuple, set)):
        raise CustomerError("bad_channels", "Choose email, WhatsApp, or both")
    channels = {str(v or "").strip().lower() for v in value}
    if not channels or not channels.issubset({"email", "whatsapp"}):
        raise CustomerError("bad_channels", "Choose email, WhatsApp, or both")
    return channels


def _ops_scan_mailer(row, link):
    return (
        fsi.EMAIL_SUBJECT.format(sender="Optiwar Support"),
        fsi.EMAIL_HTML.format(sender=fsi._html("Optiwar Support"), link=link),
        fsi.EMAIL_TEXT.format(sender="Optiwar Support", link=link),
    )


def _deliver_ops_face_scan(db, row, token, channels, email, phone,
                           mailer=None, whatsapp=None, environ=None):
    link = fsi.link_for(token, "optiwar.com")
    results = {
        "email": {"ok": False, "error": "not_requested"},
        "whatsapp": {"ok": False, "error": "not_requested", "request_id": ""},
    }
    if "email" in channels:
        subject, body_html, body_text = _ops_scan_mailer(row, link)
        try:
            (mailer or fsi._default_mailer)(
                email, subject, body_html, body_text,
                sender=fsi.DEFAULT_MAIL_SENDER)
            results["email"] = {"ok": True, "error": ""}
        except Exception as exc:
            results["email"] = {"ok": False, "error": type(exc).__name__}
    if "whatsapp" in channels:
        env = os.environ if environ is None else environ
        template = (env.get(fsi.WA_TEMPLATE_ENV) or
                    current_app.config.get(fsi.WA_TEMPLATE_ENV) or
                    fsi.DEFAULT_WA_TEMPLATE)
        components = {
            "body_1": {"type": "text", "value": "Optiwar Support"},
            "body_2": {"type": "text", "value": link},
        }
        try:
            from .notifications import send_whatsapp_tracked
            result = (whatsapp or send_whatsapp_tracked)(
                phone.lstrip("+"), template, components) or {}
            results["whatsapp"] = {
                "ok": bool(result.get("ok")),
                "error": "" if result.get("ok") else (result.get("error") or "send_failed"),
                "request_id": result.get("request_id") or "",
            }
        except Exception as exc:
            results["whatsapp"] = {
                "ok": False, "error": type(exc).__name__, "request_id": ""}

    ok = any(results[ch]["ok"] for ch in channels)
    errors = ["%s: %s" % (ch, results[ch]["error"])
              for ch in channels if not results[ch]["ok"]]
    fsi.record_ops_delivery(db, row, list(channels), ok,
                            ref=results["whatsapp"]["request_id"],
                            error="; ".join(errors))
    return results


def request_face_scan(db, customer_id, channels, created_by=None,
                      mailer=None, whatsapp=None, now=None, environ=None):
    selected = _channels(channels)
    customer = customer_by_id(db, customer_id)
    if not customer:
        raise CustomerError("not_found", "Customer not found", 404)
    email = customer["email"].strip().lower()
    phone = customer["phone"]
    if "email" in selected and not email:
        raise CustomerError("missing_email", "This customer has no stored email address")
    if "whatsapp" in selected and not phone:
        raise CustomerError("missing_phone", "This customer has no stored phone number")
    fp.ensure_schema(db)
    fsi.ensure_schema(db)
    profile = fp.ensure_self(db, customer_id, customer["name"])
    try:
        row, token = fsi.create_for_ops(
            db, customer_id, selected,
            recipient_email=email or None, recipient_phone=phone or None,
            sender_name="Optiwar Support", site_host="optiwar.com",
            created_by=created_by, now=now, environ=environ)
    except fsi.InviteError as exc:
        raise CustomerError(exc.code, str(exc), exc.status) from exc
    deliveries = _deliver_ops_face_scan(
        db, row, token, selected, email, phone, mailer=mailer,
        whatsapp=whatsapp, environ=environ)
    return {
        "request_uuid": row["request_uuid"],
        "expires_at": fp._jsonable(row["expires_at"]),
        "deliveries": deliveries,
    }


def scan_request_status(db, customer_id):
    customer = customer_by_id(db, customer_id)
    if not customer:
        raise CustomerError("not_found", "Customer not found", 404)
    fp.ensure_schema(db)
    fsi.ensure_schema(db)
    profile = fp.self_profile(db, customer_id)
    row = fsi.for_profile(db, customer_id, profile["id"]) if profile else None
    scan = None
    if profile:
        cur = db.cursor()
        cur.execute(
            "SELECT id, pd_far, pd_near, face_width, eye_mouth, decentration, "
            "recommended_diameter, recommended_bridge, recommended_length, measured_at "
            "FROM face_scans WHERE face_profile_id=%s AND status='COMPLETED' "
            "ORDER BY measured_at DESC, id DESC LIMIT 1",
            (int(profile["id"]),))
        scan = cur.fetchone()
        if scan:
            scan = {key: fp._jsonable(value) for key, value in scan.items()}
    invite = fsi.public_view(row) if row else None
    if invite:
        invite["opened_at"] = fp._jsonable(row.get("opened_at"))
        invite["completed_at"] = fp._jsonable(row.get("completed_at"))
        invite["expires_at"] = fp._jsonable(row.get("expires_at"))
    return {"customer": customer, "scan_request": invite, "latest_scan": scan}
