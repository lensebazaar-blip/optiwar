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
import re

from flask import current_app, request

TAG = "DEV_DEFECT"
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
WHERE_MAX = 120
PAGE_MAX = 200


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
    if not _CODE.match(code):
        return "", 204
    record(code, where=data.get("where"), page=data.get("page"),
           origin="browser")
    return "", 204
