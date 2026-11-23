"""Client for the Optiwar refund API.

Optiwar owns the money: this module only asks. It never touches optiwar2 and it
never decides an amount or a currency — the storefront reads both back from the
payment record and the provider, and rejects anything that disagrees.

    preview(order_id)                 what could be refunded, writes nothing
    execute(order_id, ...)            refund exactly this much, once per key
    record_upi(order_id, ...)          record a UPI payment already made by staff
    track(idempotency_key)            what happened to it, refreshed live
"""
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import requests

import config

log = logging.getLogger(__name__)

TIMEOUT = 45          # a provider refund call sits inside this one
TRACK_TIMEOUT = 20

SYMBOL = {"INR": "₹", "EUR": "€", "USD": "$", "GBP": "£"}

REASONS = (
    ("CUSTOMER_REFUND", "Customer refund request"),
    ("ORDER_CANCELLED", "Order cancelled"),
    ("OUT_OF_STOCK", "Out of stock / cannot fulfil"),
    ("RETURN_RECEIVED", "Return received"),
    ("DUPLICATE_PAYMENT", "Duplicate payment"),
    ("GOODWILL", "Goodwill gesture"),
)


class RefundApiError(Exception):
    """The storefront refused, or could not be reached. `code` is machine-readable."""

    def __init__(self, code, message, status=0, payload=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.payload = payload or {}


def base_url():
    return (os.getenv("OPTIWAR_API_BASE")
            or getattr(config, "OPTIWAR_API_BASE", "")
            or "https://in.optiwar.com").rstrip("/")


def _token():
    token = (os.getenv("OPS_REFUND_API_TOKEN")
             or getattr(config, "OPS_REFUND_API_TOKEN", ""))
    if not token:
        raise RefundApiError(
            "refund_api_not_configured",
            "OPS_REFUND_API_TOKEN is not set on this platform, so refunds "
            "cannot be sent to Optiwar. Ask Optiwar for the credential.")
    return token


def configured():
    """True when this platform holds a refund credential at all."""
    try:
        _token()
    except RefundApiError:
        return False
    return True


def _headers(operator=None, idempotency_key=None):
    headers = {"Authorization": "Bearer %s" % _token(),
               "Accept": "application/json"}
    if operator:
        headers["X-Ops-Operator"] = str(operator)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _result(response):
    if response.is_redirect or response.status_code in (301, 302, 307, 308):
        raise RefundApiError(
            "api_redirected",
            "Optiwar redirected the refund request to %s. A redirect drops the "
            "credential, so point OPTIWAR_API_BASE at the host that serves the "
            "API directly." % (response.headers.get("Location") or "another host"),
            status=response.status_code)
    try:
        payload = response.json()
    except ValueError:
        raise RefundApiError(
            "bad_gateway",
            "Optiwar answered %s with something that is not JSON"
            % response.status_code,
            status=response.status_code)

    if response.status_code >= 400 or not payload.get("ok"):
        message = payload.get("message")
        refund = payload.get("refund") or {}
        error_text = (refund.get("error_text")
                      if isinstance(refund, dict) else None)
        raw = message or error_text
        if raw and re.match(r"^provider HTTP \d{3}:\s*", str(raw)):
            detail = re.sub(r"^provider HTTP \d{3}:\s*", "", str(raw))
            message = "Razorpay refused the refund: %s" % detail
        raise RefundApiError(
            payload.get("error") or "refund_api_error",
            message or "Optiwar refused the request (HTTP %s)"
            % response.status_code,
            status=response.status_code, payload=payload)
    return payload


def new_idempotency_key(order_id):
    """One key per refund attempt, carried through review so a retried
    submission of the same reviewed draft is the same refund."""
    return "OW-%s/refund/%s" % (order_id, uuid.uuid4())


def preview(order_id):
    """Read-only. Returns the storefront's own view of what is refundable."""
    url = "%s/api/ops/orders/%s/refund/preview" % (base_url(), order_id)
    try:
        response = requests.get(url, headers=_headers(), timeout=TRACK_TIMEOUT,
                                allow_redirects=False)
    except requests.RequestException as e:
        raise RefundApiError("api_unreachable",
                             "Could not reach the Optiwar refund API: %s" % e)
    return _result(response)["preview"]


def execute(order_id, amount_minor, currency, reason_code, comment,
            idempotency_key, operator, approved_message, requested_status=None,
            notify=None):
    """Ask Optiwar to refund. Sending this twice with one key refunds once."""
    body = {
        "amount_minor": int(amount_minor),
        "currency": currency,
        "reason_code": reason_code,
        "comment": comment or "",
        "approved_message": approved_message or "",
        "notify": notify or {},
    }
    if requested_status:
        body["requested_status"] = requested_status

    url = "%s/api/ops/orders/%s/refund" % (base_url(), order_id)
    try:
        response = requests.post(
            url, json=body, timeout=TIMEOUT, allow_redirects=False,
            headers=_headers(operator=operator, idempotency_key=idempotency_key))
    except requests.RequestException as e:
        # The refund may well have been created; the key is how we find out.
        raise RefundApiError(
            "api_unreachable",
            "The refund request to Optiwar did not complete (%s). Do not retry "
            "blindly — track key %s first." % (e, idempotency_key))
    return _result(response)["refund"]


def record_upi(order_id, amount_minor, currency, reason_code, comment,
               idempotency_key, operator, approved_message, requested_status,
               upi_vpa, utr, paid_on, payee_name):
    body = {
        "amount_minor": int(amount_minor),
        "currency": currency,
        "reason_code": reason_code,
        "comment": comment or "",
        "approved_message": approved_message or "",
        "upi_vpa": upi_vpa,
        "utr": utr,
        "paid_on": paid_on,
        "payee_name": payee_name,
    }
    if requested_status:
        body["requested_status"] = requested_status

    url = "%s/api/ops/orders/%s/refund/upi" % (base_url(), order_id)
    try:
        response = requests.post(
            url, json=body, timeout=TIMEOUT, allow_redirects=False,
            headers=_headers(operator=operator, idempotency_key=idempotency_key))
    except requests.RequestException as e:
        raise RefundApiError(
            "api_unreachable",
            "The refund request to Optiwar did not complete (%s). Do not retry "
            "blindly — track key %s first." % (e, idempotency_key))
    return _result(response)["refund"]


def track(idempotency_key):
    """Live state of one refund, refreshed from the provider by the storefront."""
    url = "%s/api/ops/refunds/%s" % (base_url(), idempotency_key)
    try:
        response = requests.get(url, headers=_headers(), timeout=TRACK_TIMEOUT,
                                allow_redirects=False)
    except requests.RequestException as e:
        raise RefundApiError("api_unreachable",
                             "Could not reach the Optiwar refund API: %s" % e)
    return _result(response)["refund"]


_INVISIBLE = re.compile(r"[\s\u200b-\u200f\u2060\ufeff]+")
_UPI_LABEL = re.compile(r"^(?:(?:upi\s*id|upi|vpa)\s*[:\-]|upi\s*id\s)\s*", re.I)


def clean_upi_vpa(value):
    """Undo what copy-paste from a UPI app adds: labels, spaces, invisible marks."""
    text = _UPI_LABEL.sub("", str(value or "").strip())
    return _INVISIBLE.sub("", text).rstrip(".,;").lower()


def validate_upi(upi_vpa, utr, paid_on, payee_name):
    entered = str(upi_vpa or "").strip()
    upi_vpa = clean_upi_vpa(entered)
    if not re.fullmatch(r"[a-z0-9._-]{2,256}@[a-z][a-z0-9.-]{1,63}", upi_vpa):
        if upi_vpa and "@" not in upi_vpa:
            reason = ("needs the @handle, e.g. 9876543210@ybl or name@okhdfcbank "
                      "(copy it from the UPI app's payment details)")
        else:
            reason = "must look like name@bank, e.g. 9876543210@ybl"
        raise RefundApiError(
            "invalid_upi_id",
            "UPI ID %s: you entered \"%s\"" % (reason, entered[:80]))

    utr = _INVISIBLE.sub("", str(utr or ""))
    if not re.fullmatch(r"[0-9]{12}", utr):
        raise RefundApiError("invalid_utr", "UTR must contain exactly 12 digits")

    paid_on = str(paid_on or "").strip()
    try:
        paid_date = date.fromisoformat(paid_on)
    except (TypeError, ValueError):
        raise RefundApiError("invalid_paid_on", "Paid-on date must be YYYY-MM-DD")
    if paid_date.isoformat() != paid_on:
        raise RefundApiError("invalid_paid_on", "Paid-on date must be YYYY-MM-DD")
    today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if paid_date > today_ist or paid_date < today_ist - timedelta(days=30):
        raise RefundApiError(
            "invalid_paid_on",
            "Paid-on date must be today or within the last 30 days")

    payee_name = str(payee_name or "").strip()
    if not 2 <= len(payee_name) <= 120:
        raise RefundApiError(
            "invalid_payee_name", "Payee name must contain 2 to 120 characters")

    return {
        "upi_vpa": upi_vpa,
        "utr": utr,
        "paid_on": paid_on,
        "payee_name": payee_name,
    }


# ─── money, in integers only ───

def parse_amount_minor(text, currency):
    """'1,399.00' → 139900. Decimal, never float: a refund amount that has been
    through binary floating point is not the amount anybody agreed to."""
    raw = (text or "").strip().replace(",", "").replace(" ", "")
    for symbol in SYMBOL.values():
        raw = raw.replace(symbol, "")
    raw = raw.replace((currency or "").upper(), "").strip()
    if not raw:
        raise RefundApiError("amount_missing", "Enter a refund amount")
    try:
        major = Decimal(raw)
    except InvalidOperation:
        raise RefundApiError("amount_not_a_number",
                             "%r is not an amount" % (text or ""))
    minor = major * 100
    if minor != minor.to_integral_value():
        raise RefundApiError(
            "amount_sub_minor",
            "%s has more precision than %s can pay out" % (raw, currency or "the currency"))
    return int(minor)


def money(amount_minor, currency):
    """139900, 'INR' → '₹1,399.00'. Formatting only; the integer stays the truth."""
    amount_minor = int(amount_minor or 0)
    sign = "-" if amount_minor < 0 else ""
    amount_minor = abs(amount_minor)
    units, cents = divmod(amount_minor, 100)
    return "%s%s%s.%02d" % (sign, SYMBOL.get((currency or "").upper(), (currency or "") + " "),
                            "{:,}".format(units), cents)


def reason_label(code):
    for value, label in REASONS:
        if value == code:
            return label
    return code or ""


def message_contradicts_amount(text, amount_minor, currency):
    """True when a customer message names a money amount that is not the one
    being refunded. A message is the customer's only account of what happened,
    so a draft written for ₹999 must not go out with a ₹200 refund."""
    if not text:
        return False
    symbol = SYMBOL.get((currency or "").upper())
    pattern = r"(?:%s|%s)\s*([\d,]+(?:\.\d{1,2})?)" % (
        re.escape(symbol) if symbol else r"(?!x)x", re.escape((currency or "").upper()))
    mentioned = set()
    for raw in re.findall(pattern, text, flags=re.IGNORECASE):
        try:
            mentioned.add(parse_amount_minor(raw, currency))
        except RefundApiError:
            continue
    return bool(mentioned) and int(amount_minor) not in mentioned
