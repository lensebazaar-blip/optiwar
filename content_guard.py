"""Prohibited-content check for images customers upload (owner, 9 Nov 2026).

Every customer upload route calls ``screen`` before anything is kept, so a
refused image is never stored. The photo reader (rx_photo.py) stores nothing and
gets the same verdict from its own reading call.

* A flagged upload is refused, recorded in ``upload_flags`` (category, file
  hash, route, IP, customer: never the image), and that IP cannot upload for
  24 hours unless Ops mark the flag reviewed.
* **If an image cannot be checked it is not accepted**: the customer is asked
  to try again. These routes keep files, so an unchecked file is the risk.

Stdlib at import, so the deploy tool can read ``TABLES`` plainly.
"""
import base64
import collections
import datetime
import re

CATEGORIES = ("child_sexual", "sexual", "extremist", "illegal", "graphic")
BLOCK_HOURS = 24
MAX_PAGES = 6
ALERT_TO = "admin@optiwar.com"

Refusal = collections.namedtuple("Refusal", "code message status")
BLOCKED = Refusal("UPLOAD_BLOCKED", "This image cannot be accepted. Prohibited content is blocked, logged and may "
                  "be reported to Cyber Crime India.", 422)
IP_BLOCKED = Refusal("UPLOAD_NOT_AVAILABLE", "Uploads are not available right now. Please contact us in the chat.", 403)
UNCHECKED = Refusal("UPLOAD_CHECK_UNAVAILABLE", "We could not check this photo just now. Please try again in a minute.", 503)

PROMPT = (
    "You screen images that customers upload to an optical shop. Reply with exactly one word.\n"
    "child_sexual: any sexualised depiction of a minor. sexual: nudity or sexual content. extremist: terrorist or "
    "violent extremist material. illegal: other clearly unlawful material. graphic: graphic violence or gore. "
    "none: anything else. Ordinary photos of faces, eyeglasses, prescriptions, documents, parcels or damaged "
    "products are none."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_flags (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    created_at DATETIME NOT NULL,
    category VARCHAR(24) NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    client_ip VARCHAR(64) NOT NULL,
    customer_id INT NULL,
    door VARCHAR(24) NOT NULL,
    reviewed_at DATETIME NULL,
    reviewed_by VARCHAR(191) NULL,
    KEY ix_upload_flags_ip (client_ip, created_at),
    KEY ix_upload_flags_open (reviewed_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
TABLES = (("upload_flags", SCHEMA),)

_SCHEMA_READY = False


def ensure_schema(cursor):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    for _name, ddl in TABLES:
        cursor.execute(ddl)
    _SCHEMA_READY = True


def is_blocked(cursor, client_ip, now=None):
    """True while this IP has an unreviewed flagged upload from the last day."""
    now = now or datetime.datetime.now()
    cursor.execute("SELECT COUNT(*) AS n FROM upload_flags WHERE client_ip = %s AND reviewed_at IS NULL "
                   "AND created_at > %s", ((client_ip or "unknown")[:45], now - datetime.timedelta(hours=BLOCK_HOURS)))
    row = cursor.fetchone()
    return bool(row and row.get("n"))


def flag(cursor, category, sha256, client_ip, customer_id=None, door="photo_reader", now=None):
    """Record a refused upload. Only facts are kept: never the image."""
    cursor.execute("INSERT INTO upload_flags (created_at, category, sha256, client_ip, customer_id, door) "
                   "VALUES (%s, %s, %s, %s, %s, %s)",
                   (now or datetime.datetime.now(), category, (sha256 or "")[:64], (client_ip or "unknown")[:45],
                    customer_id, door))


def alert_text(category, door, sha256, client_ip, customer_id, when):
    """``(subject, body)`` for the alert. Facts only: the image was never kept."""
    report = ("THIS MUST BE REPORTED: file a report at https://cybercrime.gov.in or call helpline 1930.\n"
              if category == "child_sexual" else
              "Review it on /ops/controls and report it at https://cybercrime.gov.in if it is unlawful.\n")
    return ("[Optiwar ALERT] Prohibited upload refused: %s" % category,
            "A customer upload was refused as prohibited content.\n\n"
            "Category: %s\nWhere: %s\nWhen: %s\nIP address: %s\nCustomer id: %s\nFile hash (SHA-256): %s\n\n"
            "%sThe image was not stored. This IP cannot upload for %d hours unless Ops mark the flag reviewed.\n"
            % (category, door, when.strftime("%A, %d %B %Y %H:%M"), client_ip, customer_id or "guest", sha256 or "-",
               report, BLOCK_HOURS))


def alert(category, door, sha256, client_ip, customer_id=None):
    """Email admin@optiwar.com. Best effort: it must never change the refusal."""
    try:
        from .notice_mail import send_content_alert  # noqa: PLC0415
        subject, body = alert_text(category, door, sha256, client_ip, customer_id, datetime.datetime.now())
        return send_content_alert(ALERT_TO, subject, body)
    except Exception:  # noqa: BLE001
        return False


def verdict(text):
    """A category, "none", or None when the answer says neither."""
    word = re.sub(r"[^a-z_]", "", (text or "").strip().split()[0].lower()) if (text or "").strip() else ""
    return word if word in CATEGORIES or word == "none" else None


def ask_model(pages, mime):
    """One small vision call on the image(s); returns the model's text."""
    import os  # noqa: PLC0415
    from flask import current_app  # noqa: PLC0415
    from . import ai_client, lens_documents  # noqa: PLC0415
    content = [{"type": "text", "text": PROMPT}]
    for page in pages[:MAX_PAGES]:
        content.append({"type": "image_url", "image_url": {
            "url": "data:%s;base64,%s" % (mime, base64.b64encode(page).decode("ascii")), "detail": "low"}})
    workload = lens_documents.provider_name(current_app.config.get("LENS_RX_VISION_PROVIDER")
                                            or os.environ.get("LENS_RX_VISION_PROVIDER"))
    resp = ai_client.call_model(workload=workload, messages=[{"role": "user", "content": content}],
                                max_tokens=8, temperature=0, endpoint="/upload-screen")
    return resp.choices[0].message.content or ""


def _skipped():
    """Test fixtures only."""
    from flask import current_app  # noqa: PLC0415
    return bool((current_app.testing or current_app.config.get("CONTENT_GUARD_SKIP"))
                and not current_app.config.get("CONTENT_GUARD_IN_TESTS"))


def unreadable(code, message):
    """The refusal for a file a route would keep but the check cannot open."""
    return None if _skipped() else Refusal(code, message, 400)


def screen(pages, door, sha256="", mime="image/jpeg"):
    """None when the upload may be kept, else the ``Refusal`` to send back."""
    from flask import current_app, request, session  # noqa: PLC0415
    if _skipped():
        return None
    ip = (request.remote_addr or "")[:45]
    try:
        from .db import get_db  # noqa: PLC0415
        db = get_db()
        cursor = db.cursor()
        ensure_schema(cursor)
        if is_blocked(cursor, ip):
            db.commit()
            return IP_BLOCKED
        db.commit()
        answer = verdict(ask_model(pages, mime))
        if answer is None:
            current_app.logger.warning("content_guard: unclear answer door=%s", door)
            return UNCHECKED
        if answer == "none":
            return None
        current_app.logger.error("ACTIVITY:UPLOAD_FLAGGED category=%s door=%s ip=%s", answer, door, ip)
        customer = session.get("user_id") or session.get("customer_id")
        flag(cursor, answer, sha256, ip, customer, door)
        db.commit()
        alert(answer, door, sha256, ip, customer)
        return BLOCKED
    except Exception:  # noqa: BLE001 - not checked means not accepted
        current_app.logger.exception("content_guard: could not check door=%s", door)
        return UNCHECKED
