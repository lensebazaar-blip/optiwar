"""FedEx API service — refactored from CLI script for web use."""
import base64
import json
import re
import time
import unicodedata
from datetime import date, datetime
from typing import Any, Optional
from pathlib import Path

import requests
try:
    from unidecode import unidecode as _unidecode
except ImportError:
    _unidecode = None
import config
from services import invoice_series

_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def _to_ascii(text: str) -> str:
    """Transliterate Unicode text to ASCII-safe equivalent for shipping labels."""
    if not text:
        return text
    if _unidecode:
        return _unidecode(text)
    nfkd = unicodedata.normalize('NFKD', text)
    return nfkd.encode('ascii', 'ignore').decode('ascii')

SHIPPER_DEFAULTS = {
    "personName": "LensBazaar",
    "phoneNumber": "8010077770",
    "companyName": "Krishna Ecommerce Tech Pvt Ltd",
    "streetLines": [
        "Unit 418, Tower 4",
        "DLF Corporate Greens",
        "Sec 74A, SPR Gurugram 122004",
    ],
    "city": "Gurgaon",
    "stateOrProvinceCode": "HR",
    "postalCode": "122004",
    "countryCode": "IN",
}

ADDRESS_VALIDATION_SKIP_COUNTRIES = {"OM"}


class FedExError(Exception):
    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.details = details or {}


def recipient_email(order_data: dict) -> str:
    email = str(order_data.get("email") or "").strip()
    return email if _EMAIL_RE.fullmatch(email) else ""


def check_required_emails(payload: dict) -> None:
    requested_shipment = payload.get("requestedShipment") or {}
    shipper_contact = (requested_shipment.get("shipper") or {}).get("contact") or {}
    shipper_email = str(shipper_contact.get("emailAddress") or "").strip()
    if not _EMAIL_RE.fullmatch(shipper_email):
        raise FedExError(
            "Shipper email is not set — set FEDEX_SHIPPER_EMAIL before booking FedEx.")

    recipients = requested_shipment.get("recipients") or []
    recipient_contact = (recipients[0].get("contact") or {}) if recipients else {}
    customer_email = str(recipient_contact.get("emailAddress") or "").strip()
    if not _EMAIL_RE.fullmatch(customer_email):
        raise FedExError(
            "Customer email is missing or invalid. Add the customer's email to the "
            "order before booking a FedEx AWB — FedEx requires it for international shipments.")


def invoice_number(order_data: dict) -> str:
    return order_data.get("invoice_no") or invoice_series.legacy_eu(order_data)


def _http_call(method: str, url: str, headers: dict, data=None,
               json_body=None, timeout: int = 120) -> tuple:
    r = requests.request(method, url, headers=headers, data=data,
                         json=json_body, timeout=timeout)
    hdrs = {k.lower(): v for k, v in r.headers.items()}
    parsed = None
    if r.text:
        try:
            parsed = r.json()
        except Exception:
            pass
    return r.status_code, hdrs, parsed, r.text


def get_token() -> str:
    url = f"{config.FEDEX_BASE_URL}/oauth/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "client_credentials",
        "client_id": config.FEDEX_CLIENT_ID,
        "client_secret": config.FEDEX_CLIENT_SECRET,
    }
    status, _, parsed, raw = _http_call("POST", url, headers=headers, data=data, timeout=60)
    if status != 200 or not isinstance(parsed, dict) or "access_token" not in parsed:
        raise FedExError("Failed to obtain FedEx OAuth token", {"status": status, "response": raw[:2000]})
    return parsed["access_token"]


def validate_address(token: str, address: dict) -> dict:
    country = (address.get("countryCode") or "").upper().strip()
    if not country or country in ADDRESS_VALIDATION_SKIP_COUNTRIES:
        return address

    url = f"{config.FEDEX_BASE_URL}/address/v1/addresses/resolve"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-locale": "en_US",
    }
    payload = {
        "addressesToValidate": [{
            "address": {
                "streetLines": [_to_ascii(s) for s in address["streetLines"]],
                "city": _to_ascii(address.get("city", "")),
                "stateOrProvinceCode": address.get("stateOrProvinceCode", ""),
                "postalCode": address.get("postalCode", ""),
                "countryCode": country,
            }
        }]
    }

    status, _, parsed, _ = _http_call("POST", url, headers=headers, json_body=payload, timeout=60)
    if status != 200 or not isinstance(parsed, dict):
        return address

    output = parsed.get("output", {})
    resolved = output.get("resolvedAddresses", [])
    if not resolved or not isinstance(resolved[0], dict):
        return address

    a = resolved[0].get("address", {})
    updated = address.copy()
    if isinstance(a.get("streetLines"), list) and a["streetLines"]:
        updated["streetLines"] = a["streetLines"]
    for key in ["city", "stateOrProvinceCode", "postalCode", "countryCode"]:
        if a.get(key):
            updated[key] = a[key]
    return updated


def get_rate_estimate(token: str, payload: dict) -> Optional[dict]:
    url = f"{config.FEDEX_BASE_URL}/rate/v1/rates/quotes"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-locale": "en_US",
    }
    rs = payload["requestedShipment"]
    rate_payload = {
        "accountNumber": {"value": config.FEDEX_ACCOUNT_NUMBER},
        "requestedShipment": {
            "shipper": rs["shipper"],
            "recipient": rs["recipients"][0],
            "pickupType": rs.get("pickupType", "USE_SCHEDULED_PICKUP"),
            "serviceType": rs.get("serviceType", "INTERNATIONAL_PRIORITY"),
            "packagingType": rs.get("packagingType", "YOUR_PACKAGING"),
            "shippingChargesPayment": rs["shippingChargesPayment"],
            "customsClearanceDetail": rs.get("customsClearanceDetail"),
            "requestedPackageLineItems": rs["requestedPackageLineItems"],
        },
        "returnTransitTimes": True,
    }
    status, _, parsed, _ = _http_call("POST", url, headers=headers, json_body=rate_payload, timeout=120)
    if status != 200 or not isinstance(parsed, dict):
        return None
    output = parsed.get("output", {})
    rates = output.get("rateReplyDetails", [])
    if not rates:
        return None
    first = rates[0]
    amount = currency = delivery = None
    rated = first.get("ratedShipmentDetails", [])
    if rated:
        ship_rate = rated[0].get("totalNetCharge", {})
        amount = ship_rate.get("amount")
        currency = ship_rate.get("currency")
    delivery = first.get("deliveryTimestamp") or first.get("commit", {}).get("dateDetail", {}).get("dayFormat")
    return {"serviceType": first.get("serviceType"), "amount": amount, "currency": currency, "delivery": delivery}


def build_shipment_payload(order_data: dict, ship_params: dict) -> dict:
    """Build FedEx shipment payload from order data and shipping parameters."""
    shipper = SHIPPER_DEFAULTS.copy()

    raw_name = f"{order_data.get('shipping_firstname', '')} {order_data.get('shipping_lastname', '')}".strip()
    raw_company = order_data.get("shipping_company", "")
    raw_streets = [x for x in [order_data.get("shipping_address_1", ""), order_data.get("shipping_address_2", "")] if x]
    raw_city = order_data.get("shipping_city", "")

    recipient = {
        "contact": {
            "personName": _to_ascii(raw_name),
            "phoneNumber": order_data.get("telephone", ""),
            "companyName": _to_ascii(raw_company),
        },
        "address": {
            "streetLines": [_to_ascii(s) for s in raw_streets],
            "city": _to_ascii(raw_city),
            "stateOrProvinceCode": order_data.get("shipping_zone_code", ""),
            "postalCode": order_data.get("shipping_postcode", ""),
            "countryCode": order_data.get("shipping_country_code", ""),
        },
    }
    customer_email = recipient_email(order_data)
    if customer_email:
        recipient["contact"]["emailAddress"] = customer_email

    weight_kg = float(ship_params.get("weight_kg", 0.25))
    length_cm = int(ship_params.get("length_cm", 17))
    width_cm = int(ship_params.get("width_cm", 17))
    height_cm = int(ship_params.get("height_cm", 7))
    customs_amount = float(ship_params.get("customs_amount", order_data.get("total", 1)))
    currency = ship_params.get("currency", "INR")
    hs_code = ship_params.get("hs_code", "90013000")
    commodity_desc = ship_params.get("commodity_desc", "contact lens")
    country_mfg = ship_params.get("country_mfg", "US")
    qty = int(ship_params.get("qty", 1))
    is_document = ship_params.get("is_document", True)

    order_id = order_data["order_id"]
    invoice_ref = invoice_number(order_data)

    payload = {
        "labelResponseOptions": "LABEL",
        "accountNumber": {"value": config.FEDEX_ACCOUNT_NUMBER},
        "requestedShipment": {
            "shipDatestamp": str(date.today()),
            "serviceType": "INTERNATIONAL_PRIORITY",
            "packagingType": "YOUR_PACKAGING",
            "pickupType": "USE_SCHEDULED_PICKUP",
            "blockInsightVisibility": False,
            "shippingChargesPayment": {"paymentType": "SENDER"},
            "labelSpecification": {
                "imageType": "PDF",
                "labelStockType": "PAPER_85X11_TOP_HALF_LABEL",
            },
            "shipper": {
                "contact": {
                    "personName": shipper["personName"],
                    "phoneNumber": shipper["phoneNumber"],
                    "companyName": shipper["companyName"],
                    "emailAddress": config.FEDEX_SHIPPER_EMAIL,
                },
                "address": {
                    "streetLines": shipper["streetLines"],
                    "city": shipper["city"],
                    "stateOrProvinceCode": shipper["stateOrProvinceCode"],
                    "postalCode": shipper["postalCode"],
                    "countryCode": shipper["countryCode"],
                },
            },
            "recipients": [{"contact": recipient["contact"], "address": recipient["address"]}],
            "customsClearanceDetail": {
                "dutiesPayment": {"paymentType": "RECIPIENT"},
                "isDocumentOnly": bool(is_document),
                "commodities": [{
                    "description": commodity_desc,
                    "HarmonizedCode": hs_code,
                    "countryOfManufacture": country_mfg,
                    "quantity": qty,
                    "quantityUnits": "PCS",
                    "unitPrice": {"amount": f"{customs_amount:.4f}", "currency": currency},
                    "customsValue": {"amount": f"{customs_amount:.4f}", "currency": currency},
                    "weight": {"units": "KG", "value": weight_kg},
                }],
            },
            "shippingDocumentSpecification": {
                "shippingDocumentTypes": ["COMMERCIAL_INVOICE"],
                "commercialInvoiceDetail": {
                    "documentFormat": {"stockType": "PAPER_LETTER", "docType": "PDF"}
                },
            },
            "requestedPackageLineItems": [{
                "sequenceNumber": "1",
                "subPackagingType": "BOX",
                "customerReferences": [
                    {"customerReferenceType": "CUSTOMER_REFERENCE", "value": str(order_id)},
                    {"customerReferenceType": "DEPARTMENT_NUMBER", "value": f"CSB5/GST/{order_id}"},
                    {"customerReferenceType": "INVOICE_NUMBER", "value": invoice_ref},
                ],
                "weight": {"value": weight_kg, "units": "KG"},
                "dimensions": {"length": length_cm, "width": width_cm, "height": height_cm, "units": "CM"},
                "groupPackageCount": 1,
                "signatureOptionType": "SERVICE_DEFAULT",
            }],
        },
    }
    return payload


def create_shipment(token: str, payload: dict) -> dict:
    url = f"{config.FEDEX_BASE_URL}/ship/v1/shipments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-locale": "en_US",
    }
    status, hdrs, parsed, raw = _http_call("POST", url, headers=headers, json_body=payload, timeout=120)
    if status >= 300 or not isinstance(parsed, dict):
        raise FedExError(f"Shipment creation failed (HTTP {status})", {"status": status, "response": raw[:5000]})
    return parsed


def cancel_shipment(token: str, tracking_number: str) -> dict:
    url = f"{config.FEDEX_BASE_URL}/ship/v1/shipments/cancel"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-locale": "en_US",
    }
    payload = {
        "accountNumber": {"value": config.FEDEX_ACCOUNT_NUMBER},
        "trackingNumber": tracking_number,
        "deletionControl": "DELETE_ALL_PACKAGES",
    }
    status, _, parsed, raw = _http_call("PUT", url, headers=headers, json_body=payload, timeout=120)
    if status >= 300 or not isinstance(parsed, dict):
        raise FedExError(f"Shipment cancel failed (HTTP {status})", {"status": status, "response": raw[:5000]})
    return parsed


def extract_tracking_and_label(resp: dict) -> tuple:
    """Returns (tracking_number, label_base64)."""
    output = resp.get("output", {})
    txs = output.get("transactionShipments", [])
    if not txs:
        return None, None

    tx = txs[0]
    tracking = tx.get("masterTrackingNumber")
    label_b64 = None

    pieces = tx.get("pieceResponses", [])
    if pieces:
        pdocs = pieces[0].get("packageDocuments", [])
        for d in pdocs:
            for k in ["encodedLabel", "encodedDocument", "content"]:
                if d.get(k):
                    label_b64 = d[k]
                    break
            if label_b64:
                break

    if not label_b64:
        sdocs = tx.get("shipmentDocuments", [])
        for d in sdocs:
            for k in ["encodedLabel", "encodedDocument", "content"]:
                if d.get(k):
                    label_b64 = d[k]
                    break
            if label_b64:
                break

    return tracking, label_b64


def save_label_pdf(label_b64: str, order_id: int, tracking: str) -> str:
    """Save base64-encoded label PDF to disk, return path."""
    labels_dir = Path("/var/www/lbws/order-platform/labels")
    labels_dir.mkdir(parents=True, exist_ok=True)
    filename = f"label_{order_id}_{tracking}_{int(time.time())}.pdf"
    filepath = labels_dir / filename
    with open(filepath, "wb") as f:
        f.write(base64.b64decode(label_b64, validate=False))
    return str(filepath)
