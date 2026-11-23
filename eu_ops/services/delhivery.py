"""Delhivery API service — domestic (India) AWB / dispatch for Optiwar orders."""
import json
import time
from pathlib import Path

import requests

import config


class DelhiveryError(Exception):
    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.details = details or {}


def _headers(json_accept: bool = True) -> dict:
    h = {"Authorization": f"Token {config.DELHIVERY_API_TOKEN}"}
    if json_accept:
        h["Accept"] = "application/json"
    return h


def _base() -> str:
    return (config.DELHIVERY_BASE_URL or "https://track.delhivery.com").rstrip("/")


def check_serviceability(pin: str) -> dict:
    """Return the postal_code serviceability dict for a pin, or {}."""
    if not config.DELHIVERY_API_TOKEN:
        raise DelhiveryError("Delhivery API token not configured")
    url = f"{_base()}/c/api/pin-codes/json/?filter_codes={pin}"
    r = requests.get(url, headers=_headers(), timeout=30)
    if r.status_code != 200:
        raise DelhiveryError(f"Serviceability check failed (HTTP {r.status_code})",
                             {"status": r.status_code, "response": r.text[:1000]})
    data = r.json()
    codes = data.get("delivery_codes") or []
    if not codes:
        return {}
    return codes[0].get("postal_code", {})


def _pickup_location() -> dict:
    return {
        "name": config.DELHIVERY_PICKUP_LOCATION or config.PICKUP_NAME,
        "add": config.PICKUP_ADDRESS,
        "city": config.PICKUP_CITY,
        "pin_code": config.PICKUP_PIN,
        "country": config.PICKUP_COUNTRY,
        "phone": config.PICKUP_PHONE,
    }


def build_shipment(order: dict, params: dict) -> dict:
    """Build one Delhivery shipment dict from a normalized order + params."""
    name = (order.get("shipping_firstname") or order.get("firstname") or "").strip()
    if order.get("shipping_lastname"):
        name = f"{name} {order['shipping_lastname']}".strip()

    address = " ".join(x for x in [
        order.get("shipping_address_1", ""),
        order.get("shipping_address_2", ""),
    ] if x).strip()

    payment_mode = params.get("payment_mode", "Prepaid")
    total_amount = float(params.get("total_amount", order.get("total_inr", 0)) or 0)
    cod_amount = float(params.get("cod_amount", 0) or 0)
    if payment_mode == "COD" and not cod_amount:
        cod_amount = total_amount

    products_desc = (params.get("products_desc") or "Spectacle Frames")[:250]
    qty = int(params.get("qty") or sum(
        int(p.get("quantity", 1) or 1) for p in order.get("products", [])
    ) or 1)

    weight_g = int(round(float(params.get("weight_kg", 0.25)) * 1000))

    return {
        "name": name,
        "add": address,
        "pin": str(order.get("shipping_postcode", "")).strip(),
        "city": params.get("city") or order.get("shipping_city") or "",
        "state": order.get("shipping_zone") or "",
        "country": order.get("shipping_country") or "India",
        "phone": str(order.get("telephone", "")).strip(),
        "order": params.get("reference") or str(order.get("order_id", "")),
        "payment_mode": payment_mode,
        "cod_amount": f"{cod_amount:.2f}" if payment_mode == "COD" else "",
        "total_amount": f"{total_amount:.2f}",
        "products_desc": products_desc,
        "quantity": str(qty),
        "waybill": params.get("waybill", "") or "",
        "weight": str(weight_g),
        "shipment_width": str(int(params.get("width_cm", 17))),
        "shipment_height": str(int(params.get("height_cm", 7))),
        "shipment_length": str(int(params.get("length_cm", 17))),
        "shipping_mode": params.get("shipping_mode", "Surface"),
        "address_type": "home",
        **(params.get("extra_fields") or {}),
    }


def create_waybill(order: dict, params: dict) -> dict:
    """Create a Delhivery shipment (assigns waybill). Returns parsed response."""
    if not config.DELHIVERY_API_TOKEN:
        raise DelhiveryError("Delhivery API token not configured")

    shipment = build_shipment(order, params)
    payload = {
        "shipments": [shipment],
        "pickup_location": _pickup_location(),
    }
    # Form-encode via requests so '&', '#', '+' etc. inside the address JSON
    # are escaped instead of splitting the `data` field mid-string.
    body = {"format": "json", "data": json.dumps(payload)}
    url = f"{_base()}/api/cmu/create.json"

    r = requests.post(url, headers=_headers(), data=body, timeout=90)
    parsed = None
    try:
        parsed = r.json()
    except Exception:
        pass

    if r.status_code >= 300 or not isinstance(parsed, dict):
        raise DelhiveryError(f"Waybill creation failed (HTTP {r.status_code})",
                             {"status": r.status_code, "response": r.text[:3000]})

    pkgs = parsed.get("packages") or []
    ok = parsed.get("success") is True or (pkgs and pkgs[0].get("status") == "Success")
    if not ok:
        # Surface Delhivery's own error message
        rmk = "; ".join(str(x) for x in (pkgs[0].get("remarks") or [])) if pkgs else ""
        rmk = rmk or parsed.get("rmk") or ""
        raise DelhiveryError(f"Delhivery rejected the shipment: {rmk or 'unknown error'}",
                             {"response": json.dumps(parsed)[:3000]})

    return parsed


def extract_waybill(resp: dict) -> str:
    pkgs = resp.get("packages") or []
    if pkgs:
        return pkgs[0].get("waybill") or ""
    return ""


def fetch_packing_slip(waybill: str) -> dict:
    """Fetch packing slip metadata (contains a downloadable label link)."""
    url = f"{_base()}/api/p/packing_slip?wbns={waybill}&pdf=true"
    r = requests.get(url, headers=_headers(), timeout=60)
    if r.status_code != 200:
        return {}
    try:
        return r.json()
    except Exception:
        return {}


def save_label_pdf(waybill: str, order_id: str) -> str:
    """Download the shipping label PDF for a waybill; return local path or ''."""
    meta = fetch_packing_slip(waybill)
    pkgs = meta.get("packages") or []
    link = None
    if pkgs:
        link = pkgs[0].get("pdf_download_link") or pkgs[0].get("pdf")
    if not link:
        return ""
    try:
        r = requests.get(link, timeout=60)
        if r.status_code != 200 or not r.content:
            return ""
        labels_dir = Path("/var/www/lbws/order-platform/labels")
        labels_dir.mkdir(parents=True, exist_ok=True)
        safe_oid = str(order_id).replace("/", "_")
        fp = labels_dir / f"delhivery_{safe_oid}_{waybill}_{int(time.time())}.pdf"
        with open(fp, "wb") as f:
            f.write(r.content)
        return str(fp)
    except Exception:
        return ""


def cancel_waybill(waybill: str) -> dict:
    """Cancel a Delhivery shipment by waybill."""
    if not config.DELHIVERY_API_TOKEN:
        raise DelhiveryError("Delhivery API token not configured")
    url = f"{_base()}/api/p/edit.json"
    headers = _headers()
    headers["Content-Type"] = "application/json"
    body = json.dumps({"waybill": waybill, "cancellation": "true"})
    r = requests.post(url, headers=headers, data=body, timeout=60)
    parsed = None
    try:
        parsed = r.json()
    except Exception:
        pass
    if r.status_code >= 300 or not isinstance(parsed, dict):
        raise DelhiveryError(f"Cancel failed (HTTP {r.status_code})",
                             {"status": r.status_code, "response": r.text[:2000]})
    if parsed.get("status") not in (True, "true", "True"):
        raise DelhiveryError(f"Delhivery cancel failed: {parsed.get('error') or parsed.get('remark') or 'unknown'}",
                             {"response": json.dumps(parsed)[:2000]})
    return parsed


def track_status(waybill: str) -> dict:
    """Current Delhivery status of one waybill: {status, type, instructions,
    at, location}. type: PP pickup pending, PU picked up, UD/IT in transit,
    DL delivered, RT return, CN cancelled."""
    if not config.DELHIVERY_API_TOKEN:
        raise DelhiveryError("Delhivery API token not configured")
    r = requests.get(f"{_base()}/api/v1/packages/json/", params={"waybill": waybill},
                     headers=_headers(), timeout=20)
    if r.status_code != 200:
        raise DelhiveryError(f"Tracking failed (HTTP {r.status_code})",
                             {"status": r.status_code, "response": r.text[:500]})
    data = r.json()
    rows = data.get("ShipmentData") or []
    if not rows:
        return {}
    st = (rows[0].get("Shipment") or {}).get("Status") or {}
    return {"status": st.get("Status") or "", "type": st.get("StatusType") or "",
            "instructions": st.get("Instructions") or "", "at": st.get("StatusDateTime") or "",
            "location": st.get("StatusLocation") or ""}
