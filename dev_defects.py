"""Defects for the development team, as one log line each.

A failure a customer meets — on the server or in their browser — is written
to the application log as::

    ERROR ... ACTIVITY:DEV_DEFECT code=<CODE> origin=<server|browser> where=<...> page=<path>

The daily report groups these by code (``reports/dev_defects_section.py``),
so every defect becomes a line an engineer can turn into a task the next
morning, whether or not anyone reported it.

What is never in the line: a prescription value, a message text, a secret, a
customer identifier. The browser's report (``chat_gateway`` serves it at
``POST /api/chat/dev-defect``) accepts a code, a short place and a path —
nothing free-form is kept beyond those, and each is bounded.
"""
import os
import re

from flask import current_app, request
from werkzeug.security import safe_join

TAG = "DEV_DEFECT"
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
WHERE_MAX = 120
PAGE_MAX = 200
# Search-engine renderers report assets and API calls they never fetched (the
# server log shows 200/401 where they report 503): not a customer's defect.
# "Cubot" phones are customers, hence the slash or word edge after "bot".
CRAWLER = re.compile(r"[a-z]bot/|\bbot\b|spider|crawl|googleother|inspectiontool|slurp|facebookexternalhit", re.I)
# Baidu's renderer also visits as plain "Chrome/99" and reports 503s for
# requests that never reached the server. A 500 or 503 from the app is
# already recorded by the server itself (error_alerts), and nginx serves a
# /static/ file from disk without 5xx, so neither needs the browser's word.
# 502 and 504 come from nginx when the app is down or slow: still reported.
_SELF_RECORDED = {"SITE_HTTP_500", "SITE_HTTP_503"}
_ASSET_5XX = re.compile(r"^/static/(\S+) 5\d\d$")


def _unconfirmed(code, where):
    """A browser claim the server's own record shows did not happen."""
    if code in _SELF_RECORDED:
        return True
    m = _ASSET_5XX.match(str(where or "")) if code == "SITE_ASSET_FAILED" else None
    if not m:
        return False
    path = safe_join(current_app.static_folder or "", m.group(1))
    return bool(path) and os.path.isfile(path)


def _clean(value, limit):
    text = str(value or "")
    text = re.sub(r"[\r\n\t]+", " ", text)
    # A power written like -3.75 or +1.25 is not a place; drop it should one
    # ever be pasted into a message the browser forwards.
    text = re.sub(r"[+\-]\d+(\.\d+)?", "#", text)
    return text[:limit]


def record(code, where="", page=None, origin="server"):
    """Log one defect. Never raises: a broken report must not break the page."""
    try:
        if not _CODE.match(code or ""):
            code = "UNNAMED_DEFECT"
        path = page if page is not None else request.path
        current_app.logger.error(
            "ACTIVITY:%s code=%s origin=%s where=%s page=%s",
            TAG, code, origin, _clean(where, WHERE_MAX), _clean(path, PAGE_MAX))
        return True
    except Exception:  # noqa: BLE001
        return False


def browser_defect():
    """The browser's report of a defect it met; cookie session, same origin."""
    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "")
    if not _CODE.match(code) or CRAWLER.search(request.headers.get("User-Agent", "")):
        return "", 204
    if _unconfirmed(code, data.get("where")):
        current_app.logger.info("ACTIVITY:DEV_DEFECT_UNCONFIRMED code=%s where=%s",
                                code, _clean(data.get("where"), WHERE_MAX))
        return "", 204
    record(code, where=data.get("where"), page=data.get("page"),
           origin="browser")
    return "", 204
