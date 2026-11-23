"""Read-only Optiwar face-scan API access."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import os
from urllib.parse import parse_qs, quote, urlsplit

import requests

from services import reship

LIVE_ENV = "OPTIWAR_FACE_SCAN_LIVE"
TIMEOUT = 8
MAX_CAPTURE_BYTES = 5 * 1024 * 1024


class FaceScanError(Exception):
    pass


def is_live():
    return (os.getenv(LIVE_ENV) or "").strip().lower() in ("1", "true", "yes")


def fmt_mm(value):
    if value is None:
        return ""
    raw = str(value).strip()
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        return raw
    if not number.is_finite():
        return raw
    formatted = format(number, "f")
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


def fmt_measured_at(value):
    if value is None or value == "":
        return ""
    raw = str(value)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%d-%m-%Y %H:%M")
    except ValueError:
        return raw


def _order_path(order_id):
    return "/ops/api/orders/%s/face-scan" % quote(str(order_id), safe="")


def _empty_result(error="", status=None):
    return {"ok": False, "status": status, "lines": [], "warnings": [],
            "people": [], "error": error}


def fetch(order_id) -> dict:
    try:
        token = reship._token()
        if not token:
            return _empty_result("OPS_API_TOKEN not set")
        url = reship._base() + _order_path(order_id)
        response = requests.get(
            url, headers={"Authorization": "Bearer %s" % token,
                          "Accept": "application/json"}, timeout=TIMEOUT)

        try:
            data = response.json()
        except ValueError:
            if response.status_code in (401, 403):
                return _empty_result("Ops token refused (HTTP 401/403)")
            return _empty_result("HTTP %s, non-JSON reply" % response.status_code)

        if response.status_code in (401, 403):
            return _empty_result("Ops token refused (HTTP 401/403)")
        if response.status_code == 404:
            return _empty_result("Order not known to Optiwar (404)")
        if response.status_code != 200:
            return _empty_result("HTTP %s" % response.status_code)
        if not isinstance(data, dict):
            return _empty_result("unexpected response")

        status = data.get("status")
        if status not in ("SCAN_FOUND", "NO_SCAN"):
            return _empty_result("unexpected status %s" % status)

        ok = data.get("ok", True) is not False
        error = "" if ok else str(data.get("error") or "Optiwar face-scan request failed")
        return {
            "ok": ok,
            "status": status,
            "lines": data.get("lines") if isinstance(data.get("lines"), list) else [],
            "warnings": data.get("warnings") if isinstance(data.get("warnings"), list) else [],
            "people": data.get("people") if isinstance(data.get("people"), list) else [],
            "error": error,
        }
    except Exception as exc:
        return _empty_result(str(exc))


def capture(order_id, scan_id=None) -> bytes:
    try:
        token = reship._token()
        if not token:
            raise FaceScanError("OPS_API_TOKEN not set")
        url = reship._base() + _order_path(order_id) + "/capture"
        if scan_id is not None:
            url += "?scan_id=%s" % quote(str(scan_id), safe="")
        response = requests.get(
            url, headers={"Authorization": "Bearer %s" % token},
            timeout=TIMEOUT)
    except FaceScanError:
        raise
    except Exception as exc:
        raise FaceScanError(str(exc)) from exc

    try:
        if response.status_code != 200:
            raise FaceScanError("HTTP %s" % response.status_code)
        content_type = response.headers.get("Content-Type", "").strip().lower()
        if not content_type.startswith("image/jpeg"):
            raise FaceScanError("Expected image/jpeg response")
        body = response.content
        if len(body) > MAX_CAPTURE_BYTES:
            raise FaceScanError("Photo exceeds 5 MB limit")
        return body
    except FaceScanError:
        raise
    except Exception as exc:
        raise FaceScanError(str(exc)) from exc


def scan_id_from_url(url) -> int | None:
    try:
        values = parse_qs(urlsplit(str(url or "")).query).get("scan_id") or []
        value = values[0]
        return int(value) if value.isdigit() else None
    except (IndexError, TypeError, ValueError):
        return None
