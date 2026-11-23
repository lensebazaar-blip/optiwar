"""E-Invoice (IRN) and E-Way Bill service — NIC IRP1 via gst_irn library."""
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from gst_irn import Session

import config
from database import db_cursor

log = logging.getLogger(__name__)

# NIC state code for exports (Other Country)
EXPORT_STATE_CODE = "96"
EXPORT_PIN = 999999

# Default port code: Delhi Air Cargo (FedEx hub)
DEFAULT_PORT_CODE = "INDEL4"

# HSN code for contact lenses
HSN_CONTACT_LENS = "90013000"

# FedEx India GSTIN (transporter ID for e-Way Bill)
FEDEX_TRANSPORTER_ID = "27AABCF6516A1Z3"
FEDEX_TRANSPORTER_NAME = "FedEx Express Transportation and Supply Chain Services"

# Approximate distance from Gurugram warehouse to Delhi airport (km)
DEFAULT_DISTANCE_KM = 28

# Seller details (Krishna Ecommerce Technologies Pvt Ltd)
SELLER_DTLS = {
    "Gstin": "06AAHCK4567A1ZP",
    "LglNm": "Krishna Ecommerce Technologies Private Limited",
    "TrdNm": "LensBazaar Europe",
    "Addr1": "Unit 418, Tower 4, DLF Corporate Greens",
    "Addr2": "Sec 74A SPR Gurugram",
    "Loc": "Gurgaon",
    "Pin": 122004,
    "Stcd": "06",
    "Ph": "8010077770",
    "Em": "service@lensbazaar.com",
}


class EInvoiceError(Exception):
    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.details = details or {}


def _get_session() -> Session:
    """Create an authenticated gst_irn Session."""
    public_key_path = Path(config.EINV_PUBLIC_KEY_PATH)
    if not public_key_path.exists():
        raise EInvoiceError(
            f"NIC public key not found at {public_key_path}. "
            "Download it from the e-Invoice portal -> Encryption Certificates."
        )
    public_key = public_key_path.read_text()

    if not config.EINV_CLIENT_ID or not config.EINV_CLIENT_SECRET:
        raise EInvoiceError("e-Invoice Client ID / Client Secret not configured.")
    if not config.EINV_USERNAME or not config.EINV_PASSWORD:
        raise EInvoiceError("e-Invoice API username / password not configured.")

    session = Session(
        config.EINV_GSTIN,
        client_id=config.EINV_CLIENT_ID,
        client_secret=config.EINV_CLIENT_SECRET,
        username=config.EINV_USERNAME,
        password=config.EINV_PASSWORD,
        public_key=public_key,
        base_url=config.EINV_BASE_URL,
    )
    session.generate_token()
    return session


def build_irn_payload(order: dict, awb_tracking: str = "",
                      awb_date: str = "", invoice_no=None,
                      invoice_date=None) -> dict:
    """Build e-Invoice JSON payload from order data for export invoice.

    Args:
        order: Full order dict from get_order()
        awb_tracking: FedEx AWB tracking number (used as transport doc)
        awb_date: AWB creation date (DD/MM/YYYY format for NIC)
        invoice_no: Issued invoice number
        invoice_date: Issued invoice date
    """
    if not invoice_no:
        raise EInvoiceError("Issue the invoice before generating the IRN.")
    if isinstance(invoice_date, datetime):
        inv_dt = invoice_date.strftime("%d/%m/%Y")
    elif isinstance(invoice_date, date):
        inv_dt = invoice_date.strftime("%d/%m/%Y")
    elif isinstance(invoice_date, str):
        try:
            inv_dt = datetime.fromisoformat(invoice_date[:19]).strftime("%d/%m/%Y")
        except ValueError:
            raise EInvoiceError("Issue the invoice before generating the IRN.")
    else:
        raise EInvoiceError("Issue the invoice before generating the IRN.")

    # Calculate INR totals per item
    inr_subtotal = 0.0
    total_qty = 0
    item_list = []
    for idx, p in enumerate(order.get("products", []), 1):
        price_inr = round(float(p["price"]), 2)
        qty = int(p["quantity"])
        total_inr = round(float(p["total"]), 2)
        gst_rate = 5.0
        igst_amt = round(total_inr * gst_rate / 100, 2)
        total_with_gst = round(total_inr + igst_amt, 2)
        inr_subtotal += total_inr
        total_qty += qty

        item_list.append({
            "SlNo": str(idx),
            "PrdDesc": str(p["name"])[:300],
            "IsServc": "N",
            "HsnCd": HSN_CONTACT_LENS,
            "Qty": qty,
            "FreeQty": 0,
            "Unit": "BOX",
            "UnitPrice": price_inr,
            "TotAmt": total_inr,
            "Discount": 0,
            "PreTaxVal": 0,
            "AssAmt": total_inr,
            "GstRt": gst_rate,
            "IgstAmt": igst_amt,
            "CgstAmt": 0,
            "SgstAmt": 0,
            "CesRt": 0,
            "CesAmt": 0,
            "CesNonAdvlAmt": 0,
            "StateCesRt": 0,
            "StateCesAmt": 0,
            "StateCesNonAdvlAmt": 0,
            "OthChrg": 0,
            "TotItemVal": total_with_gst,
        })

    inr_subtotal = round(inr_subtotal, 2)
    total_igst = round(inr_subtotal * 5 / 100, 2)
    raw_total = inr_subtotal + total_igst
    rounded_total = round(raw_total)
    rnd_off = round(rounded_total - raw_total, 2)
    total_inv = rounded_total

    # Buyer/Ship-to details (export customer — Unregistered Person)
    ship_name = f"{order.get('shipping_firstname', '')} {order.get('shipping_lastname', '')}".strip()
    ship_addr1 = (order.get("shipping_address_1", "") or "Address")[:100]
    ship_addr2 = (order.get("shipping_address_2", "") or "")[:100]
    ship_city = (order.get("shipping_city", "") or "City")[:50]
    ship_country = (order.get("shipping_country", "") or "")[:100]
    ship_country_code = (order.get("shipping_country_code") or "").upper()[:2]
    ship_postcode = (order.get("shipping_postcode", "") or "")[:6]
    ship_zone = (order.get("shipping_zone", "") or "")[:50]
    buyer_email = (order.get("email", "") or "")[:100]
    buyer_phone = (order.get("telephone", "") or "")[:12]

    # Get EUR rate for foreign currency total
    eur_rate = float(order.get("currency_value") or order.get("eur_rate") or 0.0108)
    total_eur = round(float(order["total"]) * eur_rate, 2)

    buyer_dtls = {
        "Gstin": "URP",
        "LglNm": (ship_name or "Customer")[:100],
        "Pos": EXPORT_STATE_CODE,
        "Addr1": ship_addr1,
        "Loc": ship_city,
        "Pin": EXPORT_PIN,
        "Stcd": EXPORT_STATE_CODE,
    }
    if ship_addr2:
        buyer_dtls["Addr2"] = ship_addr2
    if buyer_phone:
        buyer_dtls["Ph"] = buyer_phone
    if buyer_email:
        buyer_dtls["Em"] = buyer_email

    ship_dtls = {
        "LglNm": (ship_name or "Customer")[:100],
        "Addr1": ship_addr1,
        "Loc": ship_city,
        "Pin": EXPORT_PIN,
        "Stcd": EXPORT_STATE_CODE,
    }
    if ship_addr2:
        ship_dtls["Addr2"] = ship_addr2

    # Dispatch from details (same as seller warehouse)
    disp_dtls = {
        "Nm": "Krishna Ecommerce Technologies Pvt Ltd",
        "Addr1": "Unit 418 Tower 4 DLF Corporate Greens",
        "Addr2": "Sec 74A SPR Gurugram",
        "Loc": "Gurgaon",
        "Pin": 122004,
        "Stcd": "06",
    }

    payload = {
        "Version": "1.1",
        "TranDtls": {
            "TaxSch": "GST",
            "SupTyp": "EXPWP",
            "RegRev": "N",
            "IgstOnIntra": "N",
        },
        "DocDtls": {
            "Typ": "INV",
            "No": invoice_no,
            "Dt": inv_dt,
        },
        "SellerDtls": SELLER_DTLS.copy(),
        "BuyerDtls": buyer_dtls,
        "DispDtls": disp_dtls,
        "ShipDtls": ship_dtls,
        "ItemList": item_list,
        "ValDtls": {
            "AssVal": inr_subtotal,
            "CgstVal": 0,
            "SgstVal": 0,
            "IgstVal": total_igst,
            "CesVal": 0,
            "StCesVal": 0,
            "Discount": 0,
            "OthChrg": 0,
            "RndOffAmt": rnd_off,
            "TotInvVal": total_inv,
            "TotInvValFc": total_eur,
        },
        "ExpDtls": {
            "ShipBNo": "",
            "ShipBDt": "",
            "Port": DEFAULT_PORT_CODE,
            "RefClm": "Y",
            "ForCur": "EUR",
            "CntCode": ship_country_code or "US",
            "ExpDuty": 0,
        },
    }

    # If AWB tracking is available, include e-Way Bill details in payload
    if awb_tracking:
        today_str = awb_date or datetime.now().strftime("%d/%m/%Y")
        payload["EwbDtls"] = {
            "TransId": FEDEX_TRANSPORTER_ID,
            "TransName": FEDEX_TRANSPORTER_NAME,
            "TransMode": "4",  # 4 = Ship/Courier (covers air courier)
            "Distance": DEFAULT_DISTANCE_KM,
            "TransDocNo": str(awb_tracking),
            "TransDocDt": today_str,
        }

    return payload


def generate_irn(order: dict, awb_tracking: str = "",
                 awb_date: str = "", invoice_no=None,
                 invoice_date=None, created_by: int = None) -> dict:
    """Generate IRN for an order. Returns dict with irn, ack_no, signed_qr_code, etc.

    If awb_tracking is provided, e-Way Bill is also generated in the same call.
    """
    order_id = order["order_id"]
    if not invoice_no:
        raise EInvoiceError("Issue the invoice before generating the IRN.")

    # Check if active IRN already exists
    existing = get_irn_for_order(order_id)
    if existing and existing["status"] == "ACT":
        raise EInvoiceError(
            f"Active IRN already exists for order #{order_id}: {existing['irn']}",
            {"irn": existing["irn"], "ack_no": existing["ack_no"]},
        )

    payload = build_irn_payload(
        order, awb_tracking=awb_tracking, awb_date=awb_date,
        invoice_no=invoice_no, invoice_date=invoice_date)
    inv_no = payload["DocDtls"]["No"]

    try:
        session = _get_session()
        result = session.generate_einvoice(payload)
    except Exception as e:
        log.error("IRN generation failed for order %s: %s", order_id, e)
        raise EInvoiceError(f"IRN generation failed: {e}")

    irn = result.get("Irn", "")
    ack_no = result.get("AckNo")
    ack_dt_str = result.get("AckDt", "")
    signed_invoice = result.get("SignedInvoice", "")
    signed_qr = result.get("SignedQRCode", "")
    status = result.get("Status", "ACT")
    ewb_no = result.get("EwbNo")
    ewb_dt_str = result.get("EwbDt", "")
    ewb_valid_str = result.get("EwbValidTill", "")

    def _parse_dt(s):
        if not s:
            return None
        try:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None

    ack_dt = _parse_dt(ack_dt_str)
    ewb_dt = _parse_dt(ewb_dt_str)
    ewb_valid = _parse_dt(ewb_valid_str)

    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO einvoice_irn
                (order_id, invoice_no, irn, ack_no, ack_dt, signed_invoice,
                 signed_qr_code, status, ewb_no, ewb_dt, ewb_valid_till,
                 api_response, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            order_id, inv_no, irn, ack_no, ack_dt,
            signed_invoice, signed_qr, status,
            ewb_no, ewb_dt, ewb_valid,
            json.dumps(result, default=str)[:65000],
            created_by,
        ))

    log.info("IRN generated for order %s: %s (Ack: %s, EWB: %s)",
             order_id, irn, ack_no, ewb_no)
    return {
        "irn": irn,
        "ack_no": ack_no,
        "ack_dt": ack_dt_str,
        "signed_qr_code": signed_qr,
        "status": status,
        "ewb_no": ewb_no,
        "ewb_dt": ewb_dt_str,
        "ewb_valid_till": ewb_valid_str,
        "invoice_no": inv_no,
    }


def cancel_irn(order_id: int, irn: str, reason: str = "1",
               remark: str = "Order cancelled") -> dict:
    """Cancel an IRN. reason: 1=Duplicate, 2=Data Entry Error, 3=Order Cancelled, 4=Others."""
    try:
        session = _get_session()
        result = session.cancel_einvoice(irn, reason, remark)
    except Exception as e:
        log.error("IRN cancellation failed for %s: %s", irn, e)
        raise EInvoiceError(f"IRN cancellation failed: {e}")

    with db_cursor() as cur:
        cur.execute("""
            UPDATE einvoice_irn
            SET status = 'CNL', cancel_reason = %s, cancel_dt = NOW()
            WHERE order_id = %s AND irn = %s
        """, (f"Reason {reason}: {remark}", order_id, irn))

    log.info("IRN cancelled for order %s: %s", order_id, irn)
    return result


def generate_ewb_by_irn(order_id: int, irn: str,
                        awb_tracking: str = "",
                        awb_date: str = "") -> dict:
    """Generate E-Way Bill using IRN (when EWB was not generated with IRN)."""
    today_str = awb_date or datetime.now().strftime("%d/%m/%Y")
    payload = {
        "Irn": irn,
        "Distance": DEFAULT_DISTANCE_KM,
        "TransMode": "4",
        "TransId": FEDEX_TRANSPORTER_ID,
        "TransName": FEDEX_TRANSPORTER_NAME,
        "TransDocNo": str(awb_tracking) if awb_tracking else "",
        "TransDocDt": today_str,
    }

    try:
        session = _get_session()
        result = session.generate_ewaybill(payload)
    except Exception as e:
        log.error("E-Way Bill generation failed for IRN %s: %s", irn, e)
        raise EInvoiceError(f"E-Way Bill generation failed: {e}")

    ewb_no = result.get("EwbNo")
    ewb_dt_str = result.get("EwbDt", "")
    ewb_valid_str = result.get("EwbValidTill", "")

    def _parse_dt(s):
        if not s:
            return None
        try:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None

    with db_cursor() as cur:
        cur.execute("""
            UPDATE einvoice_irn
            SET ewb_no = %s, ewb_dt = %s, ewb_valid_till = %s
            WHERE order_id = %s AND irn = %s
        """, (ewb_no, _parse_dt(ewb_dt_str), _parse_dt(ewb_valid_str),
              order_id, irn))

    log.info("E-Way Bill generated for order %s: EWB %s", order_id, ewb_no)
    return {
        "ewb_no": ewb_no,
        "ewb_dt": ewb_dt_str,
        "ewb_valid_till": ewb_valid_str,
    }


def get_irn_for_order(order_id: int) -> Optional[dict]:
    """Get the latest IRN record for an order."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT * FROM einvoice_irn
            WHERE order_id = %s
            ORDER BY created_at DESC LIMIT 1
        """, (order_id,))
        return cur.fetchone()


def get_qr_html(signed_qr_code: str) -> str:
    """Generate QR code HTML image from SignedQRCode string."""
    if not signed_qr_code:
        return ""
    try:
        from gst_irn.qr import get_qr_code_image_html
        return get_qr_code_image_html(signed_qr_code)
    except Exception:
        pass
    try:
        import segno
        import base64
        from io import BytesIO
        qr = segno.make(signed_qr_code, error="L")
        buf = BytesIO()
        qr.save(buf, kind="png", scale=3)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f'<img src="data:image/png;base64,{b64}" width="150" height="150" />'
    except Exception:
        return '<p style="font-size:10px;color:#999;">QR code unavailable</p>'


def get_irn_status_summary(order_id: int) -> dict:
    """Get a summary of IRN/EWB status for display in order detail."""
    irn_rec = get_irn_for_order(order_id)
    if not irn_rec:
        return {"has_irn": False}
    return {
        "has_irn": True,
        "irn": irn_rec["irn"],
        "ack_no": irn_rec["ack_no"],
        "ack_dt": irn_rec["ack_dt"],
        "status": irn_rec["status"],
        "invoice_no": irn_rec["invoice_no"],
        "ewb_no": irn_rec.get("ewb_no"),
        "ewb_dt": irn_rec.get("ewb_dt"),
        "ewb_valid_till": irn_rec.get("ewb_valid_till"),
        "qr_html": get_qr_html(irn_rec.get("signed_qr_code", "")),
        "is_active": irn_rec["status"] == "ACT",
        "is_cancelled": irn_rec["status"] == "CNL",
    }
