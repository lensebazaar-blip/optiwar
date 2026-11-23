"""Read-only Optiwar prescription-document access."""
import os
import re
from urllib.parse import quote, urljoin, urlsplit

import requests

from services import reship

LIVE_ENV = "OPTIWAR_RX_DOCS_LIVE"
FETCH_TIMEOUT = 8
PHOTO_TIMEOUT = 15
MAX_PHOTO_BYTES = 10 * 1024 * 1024
_OPS_USERNAME = re.compile(r"[A-Za-z0-9._@-]{1,64}\Z")
_REVIEW_LABELS = {
    "MATCHES_ORDER": "Matches order",
    "DIFFERENT_CONTACT_CUSTOMER": "Different, contact customer",
    "UNREADABLE_ASK_CUSTOMER": "Unreadable, ask customer",
}


class RxDocumentError(Exception):
    pass


def is_live():
    return (os.getenv(LIVE_ENV) or "").strip().lower() in ("1", "true", "yes")


def ops_user_header(user):
    username = user.get("username") if isinstance(user, dict) else None
    if isinstance(username, str) and _OPS_USERNAME.fullmatch(username):
        return {"X-Ops-User": username}
    return {}


def _order_path(order_id):
    return "/ops/api/orders/%s/rx-documents" % quote(str(order_id), safe="")


def _headers(user, accept="application/json"):
    token = reship._token()
    if not token:
        raise RxDocumentError("OPS_API_TOKEN not set")
    headers = {"Authorization": "Bearer %s" % token, "Accept": accept}
    headers.update(ops_user_header(user))
    return headers


def _request(url, user, accept="application/json", timeout=FETCH_TIMEOUT,
             stream=False, allow_redirects=True):
    try:
        return requests.get(
            url, headers=_headers(user, accept), timeout=timeout,
            stream=stream, allow_redirects=allow_redirects)
    except Exception as exc:
        raise RxDocumentError(str(exc) or exc.__class__.__name__) from exc


def _response_json(response):
    try:
        return response.json(), True
    except ValueError:
        return None, False


def _http_error(response, data, context):
    status = response.status_code
    error = data.get("error") if isinstance(data, dict) else None
    if error == "link_invalid_or_expired":
        return "Prescription photo link is invalid or expired"
    if status in (401, 403):
        return "Ops token refused (HTTP 401/403)"
    if status == 404 or error == "not_found":
        return {
            "order": "Order not known to Optiwar (404)",
            "document": "Prescription document not found",
            "photo": "Prescription photo not found (404)",
        }[context]
    if status != 200:
        return "HTTP %s" % status
    return "Optiwar prescription documents request failed"


def _checked_json(response, data=None, is_json=None, context="order"):
    if is_json is None:
        data, is_json = _response_json(response)
    if not is_json:
        raise RxDocumentError("HTTP %s, non-JSON reply" % response.status_code)
    if response.status_code != 200:
        raise RxDocumentError(_http_error(response, data, context))
    if not isinstance(data, dict):
        raise RxDocumentError("unexpected response")
    if data.get("ok") is False:
        raise RxDocumentError(_http_error(response, data, context))
    return data


def _fetch_list(order_id, user, links_zero):
    url = reship._base() + _order_path(order_id)
    if links_zero:
        url += "?links=0"
    response = _request(url, user)
    return _checked_json(response, context="order")


def _empty_result(error):
    return {"ok": False, "lines": [], "error": error}


def fetch(order_id, user) -> dict:
    try:
        data = _fetch_list(order_id, user, links_zero=True)
        lines = data.get("lines")
        return {
            "ok": True,
            "lines": lines if isinstance(lines, list) else [],
            "error": "",
        }
    except Exception as exc:
        return _empty_result(str(exc) or exc.__class__.__name__)


def document_id(document):
    if not isinstance(document, dict):
        return None
    value = document.get("document_id")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return value
    return None


def comparison(document):
    if not isinstance(document, dict):
        return ""
    value = document.get("comparison")
    return str(value).upper() if value is not None else ""


def is_purged(document):
    status = document.get("status") if isinstance(document, dict) else None
    return isinstance(status, str) and status.strip().upper() == "PURGED"


def typed_rows(document):
    values = document.get("typed") if isinstance(document, dict) else None
    values = values if isinstance(values, dict) else {}
    rows = []
    for eye, prefix in (("Right", "right"), ("Left", "left")):
        fields = (
            values.get(prefix + "_pwr"),
            values.get(prefix + "_cyl"),
            values.get(prefix + "_axis"),
            values.get(prefix + "_add"),
        )
        rows.append((eye, *(_display_value(value) for value in fields)))
    return rows


def parsed(document):
    value = document.get("parsed") if isinstance(document, dict) else None
    return value if isinstance(value, dict) else None


def parsed_rows(values):
    values = values if isinstance(values, dict) else {}
    rows = []
    for eye, key in (("Right", "right"), ("Left", "left")):
        eye_values = values.get(key)
        eye_values = eye_values if isinstance(eye_values, dict) else {}
        rows.append((eye, *(
            _display_value(eye_values.get(field))
            for field in ("sph", "cyl", "axis", "add"))))
    return rows


def review_label(document):
    verdict = document.get("review_verdict") if isinstance(document, dict) else None
    return _REVIEW_LABELS.get(verdict, "")


def source_label(document):
    source = document.get("source") if isinstance(document, dict) else None
    return "From chat" if source == "chat" else "Checkout"


def order_reference(order_id):
    value = str(order_id or "")
    return "OW-" + (value[3:] if value[:3].upper() == "OW-" else value)


def photo_pages(document):
    files = document.get("files") if isinstance(document, dict) else None
    page_numbers = []
    if isinstance(files, list):
        for file_entry in files:
            if not isinstance(file_entry, dict):
                continue
            page = _page_number(file_entry.get("page"))
            if page is not None and page not in page_numbers:
                page_numbers.append(page)
    if page_numbers:
        return page_numbers

    count = _page_number(document.get("pages")) if isinstance(document, dict) else None
    if count is not None and 0 < count <= 100:
        return list(range(count))
    return []


def _display_value(value):
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return ""


def _page_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    return None


def _origin(url):
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if (scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None):
            return None
        port = parts.port or (443 if scheme == "https" else 80)
        return scheme, parts.hostname.lower(), port
    except (TypeError, ValueError):
        return None


def _photo_url(base, link):
    base_url = base.rstrip("/") + "/"
    target = urljoin(base_url, link)
    if _origin(target) is None or _origin(target) != _origin(base_url):
        raise RxDocumentError("Prescription photo URL is not on the Optiwar API host")
    return target


def _file_for_page(files, page):
    if not isinstance(files, list):
        return None
    for file_entry in files:
        if (isinstance(file_entry, dict)
                and _page_number(file_entry.get("page")) == page):
            return file_entry
    return None


def _file_response(document_id_value, page, user):
    links_url = reship._base() + "/ops/api/rx-documents/%s/links" % quote(
        str(document_id_value), safe="")
    response = _request(links_url, user)
    data, is_json = _response_json(response)

    if response.status_code == 404 and not is_json:
        raise RxDocumentError("Optiwar links API unavailable (HTTP 404)")
    links_data = _checked_json(
        response, data=data, is_json=is_json, context="document")
    file_entry = _file_for_page(links_data.get("files"), page)

    if file_entry is None:
        raise RxDocumentError("Prescription photo page not found")
    return file_entry


def photo(order_id, document_id_value, page, user) -> tuple[bytes, str]:
    file_entry = _file_response(document_id_value, page, user)
    link = file_entry.get("view_url")
    if not isinstance(link, str) or not link.strip():
        raise RxDocumentError("Prescription photo link unavailable")

    url = _photo_url(reship._base(), link.strip())
    response = _request(
        url, user, accept="image/jpeg", timeout=PHOTO_TIMEOUT,
        stream=True, allow_redirects=False)
    try:
        if response.status_code != 200:
            data, is_json = _response_json(response)
            raise RxDocumentError(_http_error(
                response, data if is_json else None, context="photo"))
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "image/jpeg":
            raise RxDocumentError("Expected image/jpeg response")

        chunks = []
        size = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_PHOTO_BYTES:
                raise RxDocumentError("Prescription photo exceeds 10 MB limit")
            chunks.append(chunk)
        return b"".join(chunks), "image/jpeg"
    except RxDocumentError:
        raise
    except Exception as exc:
        raise RxDocumentError(str(exc) or exc.__class__.__name__) from exc
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
