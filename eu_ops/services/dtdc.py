"""DTDC API service — domestic (India) consignment / dispatch for Optiwar orders.

Uses DTDC's customer-integration (Shipsy) REST API:
  softdata   POST {base}/api/customer/integration/consignment/softdata
  label      GET  {base}/api/customer/integration/consignment/shippinglabel/stream?reference_number=CN&label_code=SHIP_LABEL_4X6
             (4x6 thermal label, same format LensBazaar prints; SHIP_LABEL_A4 = 3-copy consignment note)
             (available only once DTDC's booking sync completes — a few seconds after softdata)
  cancel     POST {base}/api/customer/integration/consignment/cancel
All calls carry the account API key in the `api-key` header.
"""
import json
import time
from pathlib import Path

import requests

import config


class DtdcError(Exception):
    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.details = details or {}


def configured() -> bool:
    return bool(config.DTDC_API_KEY and config.DTDC_CUSTOMER_CODE)


def _headers() -> dict:
    return {"api-key": config.DTDC_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json"}


def _base() -> str:
    return (config.DTDC_BASE_URL or "https://pxapi.dtdc.in").rstrip("/")


def service_type_for(shipping_mode: str, override: str = "") -> str:
    if override and override.strip():
        return override.strip()
    if (shipping_mode or "").strip().lower() == "express":
        return config.DTDC_SERVICE_EXPRESS
    return config.DTDC_SERVICE_SURFACE


def track_url(cn: str) -> str:
    return f"https://www.dtdc.in/tracking.asp?strCnno={cn}"


def _origin() -> dict:
    return {
        "name": config.DTDC_PICKUP_NAME or config.PICKUP_NAME,
        "phone": config.PICKUP_PHONE,
        "alternate_phone": "",
        "address_line_1": config.PICKUP_ADDRESS,
        "address_line_2": "",
        "pincode": config.PICKUP_PIN,
        "city": config.PICKUP_CITY,
        "state": config.PICKUP_STATE,
    }


def build_consignment(order: dict, params: dict) -> dict:
    name = (order.get("shipping_firstname") or order.get("firstname") or "").strip()
    if order.get("shipping_lastname"):
        name = f"{name} {order['shipping_lastname']}".strip()

    payment_mode = params.get("payment_mode", "Prepaid")
    total_amount = float(params.get("total_amount", order.get("total_inr", 0)) or 0)
    # DTDC rejects a 0.00 declared value (free/test/reship orders): declare a nominal 1.
    declared_value = total_amount if total_amount > 0 else 1.0
    cod_amount = float(params.get("cod_amount", 0) or 0)
    if payment_mode == "COD" and not cod_amount:
        cod_amount = total_amount

    products_desc = (params.get("products_desc") or "Spectacle Frames")[:250]
    qty = int(params.get("qty") or sum(
        int(p.get("quantity", 1) or 1) for p in order.get("products", [])
    ) or 1)
    weight_kg = float(params.get("weight_kg", 0.25) or 0.25)
    d = config.DTDC_DEFAULT_DIM_CM
    length = str(int(params.get("length_cm") or d))
    width = str(int(params.get("width_cm") or d))
    height = str(int(params.get("height_cm") or d))

    return {
        "customer_code": config.DTDC_CUSTOMER_CODE,
        "service_type_id": service_type_for(params.get("shipping_mode", "Surface"),
                                            params.get("service_type") or ""),
        "load_type": "NON-DOCUMENT",
        "consignment_type": "Forward",
        "dimension_unit": "cm",
        "length": length,
        "width": width,
        "height": height,
        "weight_unit": "kg",
        "weight": f"{weight_kg:.3f}",
        "declared_value": f"{declared_value:.2f}",
        "num_pieces": "1",
        "cod_collection_mode": "cash" if payment_mode == "COD" else "",
        "cod_amount": f"{cod_amount:.2f}" if payment_mode == "COD" else "",
        "customer_reference_number": params.get("reference") or f"OW-{order.get('order_id', '')}",
        "commodity_id": "Others",
        "description": products_desc,
        "origin_details": _origin(),
        "destination_details": {
            "name": name,
            "phone": str(order.get("telephone", "")).strip(),
            "alternate_phone": "",
            "address_line_1": (order.get("shipping_address_1") or "").strip(),
            "address_line_2": (order.get("shipping_address_2") or "").strip(),
            "pincode": str(order.get("shipping_postcode", "")).strip(),
            "city": params.get("city") or order.get("shipping_city") or "",
            "state": order.get("shipping_zone") or "",
        },
        "pieces_detail": [{
            "description": products_desc,
            "declared_value": f"{declared_value:.2f}",
            "weight": f"{weight_kg:.3f}",
            "height": height, "length": length, "width": width,
            "quantity": str(qty),
        }],
    }


def create_consignment(order: dict, params: dict) -> dict:
    """Book a DTDC consignment (assigns a CN / AWB number). Returns parsed response."""
    if not configured():
        raise DtdcError("DTDC API key / customer code not configured")

    payload = {"consignments": [build_consignment(order, params)]}
    url = f"{_base()}/api/customer/integration/consignment/softdata"
    r = requests.post(url, headers=_headers(), data=json.dumps(payload), timeout=90)
    parsed = None
    try:
        parsed = r.json()
    except Exception:
        pass
    if r.status_code >= 300 or not isinstance(parsed, dict):
        raise DtdcError(f"Consignment creation failed (HTTP {r.status_code})",
                        {"status": r.status_code, "response": r.text[:3000]})

    items = parsed.get("data") or []
    first = items[0] if items and isinstance(items[0], dict) else {}
    ok = str(parsed.get("status", "")).upper() == "OK" and first.get("success") is True \
        and first.get("reference_number")
    if not ok:
        reason = first.get("message") or first.get("reason") or parsed.get("message") \
            or parsed.get("error") or "unknown error"
        raise DtdcError(f"DTDC rejected the consignment: {reason}",
                        {"response": json.dumps(parsed)[:3000]})
    return parsed


def extract_cn(resp: dict) -> str:
    items = resp.get("data") or []
    if items and isinstance(items[0], dict):
        return items[0].get("reference_number") or ""
    return ""


def save_label_pdf(cn: str, order_id: str) -> str:
    """Download the shipping label PDF for a consignment; return local path or ''."""
    url = f"{_base()}/api/customer/integration/consignment/shippinglabel/stream?reference_number={cn}&label_code=SHIP_LABEL_4X6"
    try:
        content = b""
        for attempt in range(4):
            if attempt:
                time.sleep(3)
            r = requests.get(url, headers={"api-key": config.DTDC_API_KEY}, timeout=60)
            if r.status_code == 200 and r.content.startswith(b"%PDF"):
                content = r.content
                break
        if not content:
            return ""
        labels_dir = Path("/var/www/lbws/order-platform/labels")
        labels_dir.mkdir(parents=True, exist_ok=True)
        safe_oid = str(order_id).replace("/", "_")
        fp = labels_dir / f"dtdc_{safe_oid}_{cn}_{int(time.time())}.pdf"
        with open(fp, "wb") as f:
            f.write(content)
        return str(fp)
    except Exception:
        return ""


_trk_token = {"value": "", "at": 0.0}


def tracking_token(force: bool = False) -> str:
    """X-Access-Token for the tracking API: static from .env if given, otherwise
    fetched from DTDC's authenticate endpoint with the tracking user/password
    and cached for ~12 hours."""
    if config.DTDC_XACCESS_TOKEN:
        return config.DTDC_XACCESS_TOKEN
    if not force and _trk_token["value"] and time.time() - _trk_token["at"] < 12 * 3600:
        return _trk_token["value"]
    if not (config.DTDC_TRK_USER and config.DTDC_TRK_PASS):
        raise DtdcError("DTDC tracking credentials (DTDC_TRK_USER / DTDC_TRK_PASS) not configured")
    r = requests.get(config.DTDC_TRACK_AUTH_URL,
                     params={"username": config.DTDC_TRK_USER, "password": config.DTDC_TRK_PASS},
                     timeout=30)
    token = r.text.strip().strip('"')
    if r.status_code != 200 or not token or token.lower().startswith("<"):
        raise DtdcError(f"DTDC tracking authenticate failed (HTTP {r.status_code})",
                        {"response": r.text[:500]})
    _trk_token.update(value=token, at=time.time())
    return token


def track(cn: str) -> dict:
    """Fetch tracking detail for a consignment via DTDC's JSON tracking service."""
    headers = {"X-Access-Token": tracking_token(),
               "Content-Type": "application/json", "Accept": "application/json"}
    body = json.dumps({"trkType": "cnno", "strcnno": cn, "addtnlDtl": "Y"})
    r = requests.post(config.DTDC_TRACK_URL, headers=headers, data=body, timeout=60)
    try:
        parsed = r.json()
    except Exception:
        parsed = None
    if r.status_code >= 300 or not isinstance(parsed, dict):
        raise DtdcError(f"Tracking failed (HTTP {r.status_code})",
                        {"status": r.status_code, "response": r.text[:2000]})
    return parsed


def cancel_consignment(cn: str) -> dict:
    if not configured():
        raise DtdcError("DTDC API key / customer code not configured")
    url = f"{_base()}/api/customer/integration/consignment/cancel"
    body = json.dumps({"AWBNo": [cn], "customerCode": config.DTDC_CUSTOMER_CODE})
    r = requests.post(url, headers=_headers(), data=body, timeout=60)
    parsed = None
    try:
        parsed = r.json()
    except Exception:
        pass
    if r.status_code >= 300 or not isinstance(parsed, dict):
        raise DtdcError(f"Cancel failed (HTTP {r.status_code})",
                        {"status": r.status_code, "response": r.text[:2000]})
    if str(parsed.get("status", "")).upper() not in ("OK", "SUCCESS", "TRUE"):
        raise DtdcError(f"DTDC cancel failed: {parsed.get('message') or parsed.get('error') or 'unknown'}",
                        {"response": json.dumps(parsed)[:2000]})
    if parsed.get("success") is False:
        failures = parsed.get("failures") or []
        codes = {str(f.get("code", "")) for f in failures if isinstance(f, dict)}
        if codes and codes <= {"CONSIGNMENT_ALREADY_CANCELLED"}:
            return parsed
        msg = "; ".join(str(f.get("message") or f.get("code") or "") for f in failures
                        if isinstance(f, dict)) or "unknown"
        raise DtdcError(f"DTDC cancel failed: {msg}", {"response": json.dumps(parsed)[:2000]})
    return parsed
