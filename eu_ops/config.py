"""Application configuration."""
import os
import secrets

# Database
DB_HOST = os.getenv("OSLB6_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("OSLB6_DB_PORT", "3306"))
DB_USER = os.getenv("OSLB6_DB_USER", "")
DB_PASS = os.getenv("OSLB6_DB_PASS", "")
DB_NAME = os.getenv("OSLB6_DB_NAME", "oslb6")

# Optiwar DB (remote server: 172.105.54.11)
OW_DB_HOST = os.getenv("OW_DB_HOST", "172.105.54.11")
OW_DB_PORT = int(os.getenv("OW_DB_PORT", "3306"))
OW_DB_USER = os.getenv("OW_DB_USER", "")
OW_DB_PASS = os.getenv("OW_DB_PASS", "")
OW_DB_NAME = os.getenv("OW_DB_NAME", "optiwar2")

# Optiwar write account — INSERT only, on the six tables an order needs. No
# default: without both values the platform cannot write to optiwar2 at all and
# order creation falls back to handing the operator a script.
OW_WRITE_DB_USER = os.getenv("OW_WRITE_DB_USER", "")
OW_WRITE_DB_PASS = os.getenv("OW_WRITE_DB_PASS", "")

# JWT
SECRET_KEY = os.getenv("OPP_SECRET_KEY", secrets.token_hex(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 480  # 8 hours

# OTP
OTP_EXPIRE_MINUTES = 10
OTP_LENGTH = 6

# SMTP for OTP emails
SMTP_HOST = os.getenv("SMTP_HOST", "mail.ket.ltd")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "service@lensbazaar.com")

# FedEx
FEDEX_BASE_URL = os.getenv("FEDEX_BASE_URL", "https://apis.fedex.com")
FEDEX_CLIENT_ID = os.getenv("FEDEX_CLIENT_ID", "")
FEDEX_CLIENT_SECRET = os.getenv("FEDEX_CLIENT_SECRET", "")
FEDEX_ACCOUNT_NUMBER = os.getenv("FEDEX_ACCOUNT_NUMBER", "")
FEDEX_SHIPPER_EMAIL = os.getenv("FEDEX_SHIPPER_EMAIL", "admin@lensbazaar.com")

# e-Invoice / IRN (NIC IRP1 via GSP)
EINV_BASE_URL = os.getenv("EINV_BASE_URL", "https://einvoice1.gst.gov.in")
EINV_CLIENT_ID = os.getenv("EINV_CLIENT_ID", "")
EINV_CLIENT_SECRET = os.getenv("EINV_CLIENT_SECRET", "")
EINV_USERNAME = os.getenv("EINV_USERNAME", "")
EINV_PASSWORD = os.getenv("EINV_PASSWORD", "")
EINV_GSTIN = "06AAHCK4567A1ZP"
EINV_PUBLIC_KEY_PATH = os.getenv("EINV_PUBLIC_KEY_PATH", "/var/www/lbws/order-platform/keys/einv_public.pem")

# WhatsApp Business API (Meta)
WA_SYSTEM_TOKEN = os.getenv("WA_SYSTEM_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "100670912808722")
WA_WABA_ID = os.getenv("WA_WABA_ID", "104941939038039")
WA_API_VERSION = os.getenv("WA_API_VERSION", "v18.0")

# Optiwar WhatsApp via msg91 (separate WABA for optiwar.com)
MSG91_AUTHKEY = os.getenv("MSG91_AUTHKEY", "")
MSG91_INTEGRATED_NUMBER = os.getenv("MSG91_INTEGRATED_NUMBER", "919355380318")
MSG91_WA_NAMESPACE = os.getenv("MSG91_WA_NAMESPACE", "465b6c2b_e349_4e3b_88b7_233fbe00750e")

# Razorpay — env only; live keys must never sit in source
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

# Twilio SMS
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")

# DeepSeek AI (for support email drafting)
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

# App
APP_HOST = os.getenv("OPP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("OPP_PORT", "8500"))
APP_TITLE = "LensBazaar Order Processing"

# Order statuses (from DB)
ORDER_STATUSES = {
    1: "Pending",
    2: "Processing",
    3: "Shipped",
    5: "Complete",
    7: "Canceled",
    8: "Denied",
    9: "Canceled Reversal",
    10: "Failed",
    11: "Refunded",
    12: "Reversed",
    13: "Chargeback",
    14: "Expired",
    15: "Processed",
    16: "Voided",
    17: "confirm on COD",
    18: "Processing Shipment",
    19: "Packed",
    20: "Payment Pending",
    21: "Partially pending",
    22: "Partially Refund",
    23: "Partially Shipped",
    24: "Delivery-Assist",
    25: "Support-Requested",
    26: "Partially-Complete",
}

# Inventory DB (192.168.0.176 - WMS)
INV_DB_HOST = os.getenv("INV_DB_HOST", "192.168.0.176")
INV_DB_PORT = int(os.getenv("INV_DB_PORT", "3306"))
INV_DB_USER = os.getenv("INV_DB_USER", "")
INV_DB_PASS = os.getenv("INV_DB_PASS", "")
INV_DB_NAME = os.getenv("INV_DB_NAME", "inventory")

# LOT Number Edit Passcode
LOT_EDIT_PASSCODE = os.getenv("LOT_EDIT_PASSCODE", "")

# Delhivery API (domestic India dispatch — Optiwar orders)
DELHIVERY_API_TOKEN = os.getenv("DELHIVERY_API_TOKEN", "")
DELHIVERY_CLIENT_NAME = os.getenv("DELHIVERY_CLIENT_NAME", "")
DELHIVERY_PICKUP_LOCATION = os.getenv("DELHIVERY_PICKUP_LOCATION", "")
DELHIVERY_BASE_URL = os.getenv("DELHIVERY_BASE_URL", "https://track.delhivery.com")
PICKUP_NAME = os.getenv("PICKUP_NAME", "")
PICKUP_PIN = os.getenv("PICKUP_PIN", "")
PICKUP_ADDRESS = os.getenv("PICKUP_ADDRESS", "")
PICKUP_PHONE = os.getenv("PICKUP_PHONE", "")
PICKUP_CITY = os.getenv("PICKUP_CITY", "")
PICKUP_STATE = os.getenv("PICKUP_STATE", "")
PICKUP_COUNTRY = os.getenv("PICKUP_COUNTRY", "India")

# DTDC API (domestic India dispatch — second courier for Optiwar orders)
DTDC_API_KEY = os.getenv("DTDC_API_KEY", "")
DTDC_CUSTOMER_CODE = os.getenv("DTDC_CUSTOMER_CODE", "")
# DTDC_LIVE=1 -> production endpoints/services, 0 -> DTDC staging (Shipsy alpha demo)
DTDC_LIVE = os.getenv("DTDC_LIVE", "1").strip().lower() in ("1", "true", "yes")
_DTDC_ENV = {
    True: dict(base="https://pxapi.dtdc.in",
               trk_auth="https://blktracksvc.dtdc.com/dtdc-api/api/dtdc/authenticate",
               trk="https://blktracksvc.dtdc.com/dtdc-api/rest/JSONCnTrk/getTrackDetails",
               surface="B2C SMART EXPRESS", express="B2C PRIORITY"),
    False: dict(base="https://alphademodashboardapi.shipsy.io",
                trk_auth="https://dtdcstagingapi.dtdc.com/dtdc-api/api/dtdc/authenticate",
                trk="https://dtdcstagingapi.dtdc.com/dtdc-tracking-api/dtdc-api/rest/JSONCnTrk/getTrackDetails",
                surface="GROUND EXPRESS", express="PRIORITY"),
}[DTDC_LIVE]
# Booking / label / cancel base; tracking auth + tracking; service type per shipment mode.
# Each may be overridden explicitly in .env, otherwise follows DTDC_LIVE.
DTDC_BASE_URL = os.getenv("DTDC_BASE_URL", _DTDC_ENV["base"])
DTDC_TRACK_AUTH_URL = os.getenv("DTDC_TRACK_AUTH_URL", _DTDC_ENV["trk_auth"])
DTDC_TRACK_URL = os.getenv("DTDC_TRACK_URL", _DTDC_ENV["trk"])
DTDC_SERVICE_SURFACE = os.getenv("DTDC_SERVICE_SURFACE", _DTDC_ENV["surface"])
DTDC_SERVICE_EXPRESS = os.getenv("DTDC_SERVICE_EXPRESS", _DTDC_ENV["express"])
# Tracking credentials: token is fetched from the authenticate endpoint with user/pass;
# DTDC_XACCESS_TOKEN may be set to skip the auth call.
DTDC_TRK_USER = os.getenv("DTDC_TRK_USER", "")
DTDC_TRK_PASS = os.getenv("DTDC_TRK_PASS", "")
DTDC_XACCESS_TOKEN = os.getenv("DTDC_XACCESS_TOKEN", "")
DTDC_PICKUP_NAME = os.getenv("DTDC_PICKUP_NAME", "")
DTDC_DEFAULT_DIM_CM = int(os.getenv("DTDC_DEFAULT_DIM_CM", "10") or 10)

COURIERS = ("Delhivery", "DTDC")


def courier_track_url(courier: str, tracking: str) -> str:
    if (courier or "").strip().lower() == "dtdc":
        return f"https://www.dtdc.in/tracking.asp?strCnno={tracking}"
    return f"https://www.delhivery.com/track/package/{tracking}"

# Optiwar customer notification sender (dispatch emails go out as this address)
OPTIWAR_FROM_EMAIL = os.getenv("OPTIWAR_FROM_EMAIL", "admin@optiwar.com")
OPTIWAR_ADMIN_CC = os.getenv("OPTIWAR_ADMIN_CC", "admin@optiwar.com")
# Default accounts-team recipient for refund summaries (admin is always CC'd)
OPTIWAR_ACCOUNTS_EMAIL = os.getenv("OPTIWAR_ACCOUNTS_EMAIL", "accounts@optiwar.com")
# Optional dedicated SMTP for admin@optiwar.com (falls back to main SMTP if unset)
OPTIWAR_SMTP_HOST = os.getenv("OPTIWAR_SMTP_HOST", "")
OPTIWAR_SMTP_PORT = int(os.getenv("OPTIWAR_SMTP_PORT", "0") or 0)
OPTIWAR_SMTP_USER = os.getenv("OPTIWAR_SMTP_USER", "")
OPTIWAR_SMTP_PASS = os.getenv("OPTIWAR_SMTP_PASS", "")

# Optiwar GST tax invoice (same seller entity as EU: Krishna Ecommerce Technologies Pvt Ltd)
OPTIWAR_GSTIN = os.getenv("OPTIWAR_GSTIN", "06AAHCK4567A1ZP")   # seller GSTIN (06 = Haryana)
OPTIWAR_SELLER_STATE = os.getenv("OPTIWAR_SELLER_STATE", "Haryana")
OPTIWAR_GST_RATE = float(os.getenv("OPTIWAR_GST_RATE", "5"))    # % applicable
OPTIWAR_HSN_FRAME = os.getenv("OPTIWAR_HSN_FRAME", "9003 19 00")   # spectacle frames (other materials)
OPTIWAR_HSN_LENS = os.getenv("OPTIWAR_HSN_LENS", "9001 50 00")     # spectacle lenses (other materials)

# Reconciliation ledger (read-only view of mergers_database on the WMS box)
RECON_DB_HOST = os.getenv("RECON_DB_HOST", "192.168.0.176")
RECON_DB_PORT = int(os.getenv("RECON_DB_PORT", "3306"))
RECON_DB_USER = os.getenv("RECON_DB_USER", "")
RECON_DB_PASS = os.getenv("RECON_DB_PASS", "")
RECON_DB_NAME = os.getenv("RECON_DB_NAME", "mergers_database")

# Reconciliation runner (the only writer; lives on the WMS box)
RECON_RUNNER_HOST = os.getenv("RECON_RUNNER_HOST", "192.168.0.176")
RECON_RUNNER_PORT = int(os.getenv("RECON_RUNNER_PORT", "8600"))
RECON_RUNNER_TOKEN = os.getenv("RECON_RUNNER_TOKEN", "")
