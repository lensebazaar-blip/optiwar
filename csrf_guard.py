"""CSRF Phase 1: Origin/Referer verification for cookie-authenticated,
state-changing requests.

Phase 1 runs in LOG-ONLY mode (``CSRF_ENFORCE = False``): it never blocks a
request, it only records a structured decision so we can confirm no legitimate
traffic would be rejected before turning on enforcement.

Decision logic (for POST/PUT/DELETE/PATCH only):
- The request's own host is always trusted (same-origin).
- Origin is preferred; if absent, the Referer host is used.
- If the source host is in the trusted set  -> allow.
- If a source host is present but not trusted -> cross-origin (would block).
- If neither Origin nor Referer is present    -> missing (would block).

Exemptions are EXACT endpoints only (never blueprint-wide, so a future browser
route added to the same blueprint does not silently inherit an exemption). Each
exempt endpoint must carry its own non-cookie authentication:
- ``main.payment_callback`` (Paytm cross-site provider form POST): provider
  checksum + idempotent processing.
- ``chat_gateway.chat_agent_reply`` (KET agent reply push): HMAC signature.
- ``chat_gateway.chat_resolve`` (KET resolve push; also served to the widget
  owner via signed-cookie ownership check): HMAC signature for the server path.
- ``main.razorpay_webhook`` (Razorpay server-to-server delivery, no browser and
  so never any Origin/Referer): HMAC signature over the raw body + idempotent
  processing.
NOTE: ``main.razorpay_verify`` is intentionally NOT exempt -- it is a browser
POST that carries the customer session cookie, so it must pass the Origin/
Referer check (and later a CSRF token) in addition to the Razorpay signature.

Logging avoids full URLs/query strings, form bodies, raw cookies, customer
identifiers and full user-agents -- only coarse, non-PII fields.
"""
import uuid
from urllib.parse import urlsplit

from flask import current_app, g, request, session

CSRF_METHODS = {"POST", "PUT", "DELETE", "PATCH"}

# EXACT server-to-server endpoints only (each has its own non-cookie auth).
CSRF_EXEMPT_ENDPOINTS = {
    "main.payment_callback",           # Paytm /payment/callbackurl  (checksum + idempotency)
    "main.razorpay_webhook",           # Razorpay /razorpay/webhook  (HMAC over raw body + idempotency)
    "chat_gateway.chat_agent_reply",   # KET agent reply push  (HMAC)
    "chat_gateway.chat_resolve",       # KET resolve push  (HMAC; widget path uses cookie ownership)
    "crm.ket_ticket_event",            # KET ticket-lifecycle push (resolved/reopened)  (HMAC)
    "crm.msg91_delivery_event",        # MSG91 delivery-status callback  (optional token)
    "crm.support_preferences",         # customer notification-pref API (no cookie auth; keyed by email/phone)
    "main.ops_refund_execute",         # EU Ops refund API  (scoped Bearer credential + server-side idempotency)
}


def _host(url):
    if not url:
        return None
    try:
        netloc = urlsplit(url).netloc
    except Exception:
        return None
    if not netloc:
        return None
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    if netloc.endswith("]"):  # bare IPv6, no port
        host = netloc
    else:
        host = netloc.rsplit(":", 1)[0] if ":" in netloc else netloc
    return host.lower().strip("[]") or None


def evaluate(origin, referer, request_host, trusted_hosts):
    """Pure decision function -> (decision, would_block)."""
    trusted = {h.lower() for h in trusted_hosts if h}
    rh = _host(request_host)
    if rh:
        trusted.add(rh)  # same-origin is always trusted

    src = _host(origin) if origin else _host(referer)
    if src is None:
        return "missing-origin-referer", True
    if src in trusted:
        return "allow", False
    return "cross-origin", True


def _is_exempt(endpoint):
    return bool(endpoint) and endpoint in CSRF_EXEMPT_ENDPOINTS


def _ctype_category(ct):
    ct = (ct or "").split(";", 1)[0].strip().lower()
    if not ct:
        return "none"
    if ct == "application/x-www-form-urlencoded":
        return "form"
    if ct == "multipart/form-data":
        return "multipart"
    if ct == "application/json":
        return "json"
    return "other"


def _ua_family(ua):
    ua = (ua or "").lower()
    if not ua:
        return "none"
    for token in ("curl", "python", "wget", "postman", "bot", "spider", "crawl"):
        if token in ua:
            return token
    if "edg" in ua:
        return "edge"
    if "chrome" in ua or "crios" in ua:
        return "chrome"
    if "firefox" in ua or "fxios" in ua:
        return "firefox"
    if "safari" in ua:
        return "safari"
    return "other"


def init_csrf_guard(app):
    app.config.setdefault("CSRF_ENFORCE", False)  # Phase 1 = log-only

    # Assign a request id early (before the guard) so rid=- is rare.
    @app.before_request
    def _assign_request_id():
        rid = request.headers.get("X-Request-ID")
        if not rid:
            rid = uuid.uuid4().hex[:12]
        g.request_id = rid
        return None

    @app.before_request
    def _csrf_origin_referer_guard():
        if request.method not in CSRF_METHODS:
            return None
        endpoint = request.endpoint or ""
        if _is_exempt(endpoint):
            return None

        decision, would_block = evaluate(
            request.headers.get("Origin"),
            request.headers.get("Referer"),
            request.host_url,
            current_app.config.get("TRUSTED_HOSTS", []),
        )
        enforce = bool(current_app.config.get("CSRF_ENFORCE", False))

        if would_block:
            src = _host(request.headers.get("Origin")) or _host(request.headers.get("Referer")) or "-"
            current_app.logger.warning(
                "csrf-guard mode=%s reason=%s endpoint=%s method=%s site=%s "
                "src_host=%s authed=%s cookie=%s ctype=%s ua=%s rid=%s",
                "enforce" if enforce else "log-only",
                decision, endpoint, request.method, _host(request.host_url) or "-",
                src, bool(session.get("user_id")), bool(request.cookies),
                _ctype_category(request.content_type),
                _ua_family(request.headers.get("User-Agent")),
                getattr(g, "request_id", "-"),
            )
            if enforce:
                from flask import abort
                abort(403, description="CSRF origin check failed")
        return None
