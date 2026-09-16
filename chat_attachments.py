"""
A photo a customer attaches in the AI chat, kept by Optiwar and shown to KET.

The customer's browser never talks to KET: it posts the file to Optiwar, which
validates the bytes (not the declared type), stores them outside the web root,
records a row here and writes a "[Photo: name]" line into the transcript. When
the conversation becomes a KET ticket the photos not yet forwarded travel in
the create call (``images[]``, base64), which is the only path KET's first-pass
auto-resolve sees; a photo attached after the ticket exists is uploaded to
``/messages/{ticket_uid}/attachments`` as multipart. Every forward is recorded
on the row (``ket_status``) so a photo is sent once and a failure is visible.

Stdlib only at import: deploy/deploy.py reads TABLES / SESSION_COLUMNS from
here for the migration plan.
"""

import base64
import hashlib
import os

ACCEPTED = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}
ACCEPTED_MIME = frozenset(ACCEPTED.values())
MAX_BYTES = 8 * 1024 * 1024          # KET: 8 MB per image
MIN_BYTES = 64
MAX_PER_SESSION = 8                  # stored; KET describes the first 4
VISION_ANALYSED = 4
FIELD = "file"

KET_PENDING = "pending"              # stored, no KET ticket yet
KET_SENT = "sent"                    # forwarded (create call or attachments endpoint)
KET_FAILED = "failed"                # forward attempted, KET refused / unreachable

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_attachments (
    id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    session_id     VARCHAR(64) NOT NULL,
    message_id     BIGINT UNSIGNED NULL,
    filename       VARCHAR(191) NOT NULL,
    mime_type      VARCHAR(64) NOT NULL,
    byte_size      INT UNSIGNED NOT NULL,
    sha256         CHAR(64) NOT NULL,
    stored_name    VARCHAR(191) NOT NULL,
    ket_status     VARCHAR(16) NOT NULL DEFAULT 'pending',
    ket_via        VARCHAR(16) NULL,
    ket_ticket_uid VARCHAR(191) NULL,
    ket_error      VARCHAR(255) NULL,
    ket_sent_at    DATETIME NULL,
    vision_json    TEXT NULL,
    vision_model   VARCHAR(64) NULL,
    vision_error   VARCHAR(255) NULL,
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_ca_session (session_id, created_at),
    KEY idx_ca_status (ket_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (("chat_attachments", SCHEMA),)

# The KET ticket a chat became, kept on the session so a later photo knows
# where to go. The mapping table keys on the Optiwar ticket, not the session.
SESSION_COLUMNS = (
    ("chat_sessions", (
        ("ket_ticket_uid", "VARCHAR(191) NULL"),
        ("ket_ticket_ref", "VARCHAR(191) NULL"),
    )),
    # What the vision model saw (JSON), which model, or why it could not look.
    ("chat_attachments", (
        ("vision_json", "TEXT NULL"),
        ("vision_model", "VARCHAR(64) NULL"),
        ("vision_error", "VARCHAR(255) NULL"),
    )),
)


def ensure_schema(cursor):
    for _name, ddl in TABLES:
        cursor.execute(ddl)
    for table, columns in SESSION_COLUMNS:
        for name, decl in columns:
            try:
                cursor.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
            except Exception as e:  # noqa: BLE001 - 1060: the column is already there
                if "1060" not in str(e) and "Duplicate column" not in str(e):
                    raise


class Rejected(Exception):
    """A refusal the customer is told about in these words."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def kind_of(data):
    """``jpeg`` / ``png`` / ``gif`` / ``webp`` from the bytes, or None."""
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def safe_filename(name):
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = "".join(ch for ch in base if ch.isalnum() or ch in "._- ")[:120]
    return base or "photo"


def validate(data, filename=""):
    """The stored record for accepted bytes, or ``Rejected``.

    Returns ``{"kind", "mime_type", "filename", "sha256", "byte_size"}``. The
    file name is only ever a label; the type comes from the bytes and the
    stored name is derived from the row id.
    """
    size = len(data or b"")
    if size < MIN_BYTES:
        raise Rejected("ATTACHMENT_EMPTY", "That file is empty. Please choose a photo.")
    if size > MAX_BYTES:
        raise Rejected("ATTACHMENT_TOO_LARGE",
                       "That photo is over 8 MB. Please send a smaller one.")
    kind = kind_of(data)
    if kind is None:
        raise Rejected("ATTACHMENT_TYPE",
                       "Only JPEG, PNG, GIF or WebP photos can be attached.")
    name = safe_filename(filename)
    if "." not in name:
        name = "%s.%s" % (name, "jpg" if kind == "jpeg" else kind)
    return {"kind": kind, "mime_type": ACCEPTED[kind], "filename": name,
            "sha256": hashlib.sha256(data).hexdigest(), "byte_size": size}


def stored_name(attachment_id, kind):
    """The file name a photo is kept under; nothing of the customer's in it."""
    return "chatimg-%d.%s" % (int(attachment_id), "jpg" if kind == "jpeg" else kind)


def transcript_line(filename):
    """What the transcript (and the model) sees in place of the photo."""
    return "[Photo attached: %s]" % filename


def ket_images(rows_with_bytes):
    """KET ``images[]`` for the create call from ``[(row, bytes), ...]``.

    Standard padded base64, no data-URI prefix. The order is the order the
    customer sent them, so KET's first-four vision rule describes the photos
    the customer led with.
    """
    out = []
    for row, data in rows_with_bytes:
        out.append({
            "filename": row["filename"],
            "mime_type": row["mime_type"],
            "data_base64": base64.b64encode(data).decode("ascii"),
        })
    return out


def redact_for_log(payload):
    """The KET payload with image bytes replaced by their size, for a log line."""
    if not isinstance(payload, dict) or "images" not in payload:
        return payload
    copy = dict(payload)
    copy["images"] = [
        {"filename": i.get("filename"), "mime_type": i.get("mime_type"),
         "base64_len": len(i.get("data_base64") or "")}
        for i in payload["images"]]
    return copy
