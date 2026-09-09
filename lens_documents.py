"""A prescription the customer photographed, read by a model, confirmed by them.

The fourth door onto the lens page's eye cards (after typing, Saved and Ask
AI), and like the other three it ends in the same place: values proposed into
the cards, checked and confirmed by the customer, validated by the server on
Add to Cart. Nothing is ordered from a picture. What this module adds is the
document itself — kept, owned, auditable — and the reading of it.

Three facts shape the design:

* **The original is evidence.** It is normalised (orientation fixed, size
  bounded, re-encoded — so a stored file is a JPEG this application made, not
  a customer's bytes) and kept outside the web root with a retention date;
  Ops reach it only through a signed, expiring link, and every issue and
  every download is an audit row.
* **The reading is a proposal.** The vision provider returns values the lens's
  own validator screens against the product before they reach the page; a
  value the lens does not make is a refusal, not a default. What the model
  saw is stored as typed values, never as free text.
* **The provider is a setting.** ``LENS_RX_VISION_PROVIDER`` names an
  ``ai_client`` workload (``openai_vision`` live, ``deepseek_vision`` ready);
  both are asked the same question and answer the same JSON, so switching is
  a config change with no code path of its own.

Stdlib + Pillow only at import; the model call and the routes live where
Flask does (``lens_upload.py``).
"""
import datetime
import hashlib
import io
import json
import re

try:
    from . import lens_order, lens_rx
except ImportError:  # run as a plain module (tests, deploy tool, scripts)
    import lens_order
    import lens_rx

# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

ST_UPLOADED = "UPLOADED"
ST_PARSED = "PARSED"
ST_UNREADABLE = "UNREADABLE"
ST_INCOMPATIBLE = "INCOMPATIBLE"
ST_CONFIRMED = "CONFIRMED"
ST_PURGED = "PURGED"
STATUSES = (ST_UPLOADED, ST_PARSED, ST_UNREADABLE, ST_INCOMPATIBLE,
            ST_CONFIRMED, ST_PURGED)

RETENTION_MONTHS = lens_rx.RETENTION_MONTHS

SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_documents (
    document_id     INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    customer_id     INT NOT NULL,
    product_id      INT NOT NULL,
    site            VARCHAR(32) NULL,
    status          VARCHAR(16) NOT NULL,
    stored_name     VARCHAR(80) NULL,
    original_kind   VARCHAR(8) NOT NULL,
    original_bytes  INT UNSIGNED NOT NULL,
    stored_bytes    INT UNSIGNED NOT NULL DEFAULT 0,
    sha256          CHAR(64) NOT NULL,
    pages           TINYINT UNSIGNED NOT NULL DEFAULT 1,
    provider        VARCHAR(32) NULL,
    model           VARCHAR(64) NULL,
    parsed_json     TEXT NULL,
    confidence      DECIMAL(3,2) NULL,
    refusal         VARCHAR(255) NULL,
    ket_ref         VARCHAR(64) NULL,
    created_at      DATETIME NOT NULL,
    parsed_at       DATETIME NULL,
    confirmed_at    DATETIME NULL,
    retain_until    DATE NOT NULL,
    purged_at       DATETIME NULL,
    KEY ix_cldoc_customer (customer_id, created_at),
    KEY ix_cldoc_status (status, created_at),
    KEY ix_cldoc_retain (retain_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_document_audit (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    document_id INT UNSIGNED NOT NULL,
    action      VARCHAR(32) NOT NULL,
    actor       VARCHAR(191) NULL,
    ip          VARCHAR(64) NULL,
    detail      VARCHAR(255) NULL,
    occurred_at DATETIME NOT NULL,
    KEY ix_cldoca_doc (document_id, occurred_at),
    KEY ix_cldoca_action (action, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (("contact_lens_documents", SCHEMA),
          ("contact_lens_document_audit", AUDIT_SCHEMA))

# Audit actions. The customer's own upload and confirmation are in the trail
# too, so a document's history starts with the person it belongs to.
AUD_UPLOADED = "UPLOADED"
AUD_PARSED = "PARSED"
AUD_PARSE_FAILED = "PARSE_FAILED"
AUD_CONFIRMED = "CONFIRMED"
AUD_LINK_ISSUED = "LINK_ISSUED"
AUD_DOWNLOADED = "DOWNLOADED"
AUD_KET_REF = "KET_REF"
AUD_PURGED = "PURGED"

# --------------------------------------------------------------------------
# the file
# --------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MIN_UPLOAD_BYTES = 1024
MAX_SIDE_PX = 2000
MAX_PDF_PAGES = 2
PDF_RENDER_DPI = 150
JPEG_QUALITY = 85

KIND_JPEG, KIND_PNG, KIND_WEBP, KIND_HEIC, KIND_PDF = (
    "jpeg", "png", "webp", "heic", "pdf")

# Detected from the bytes, never from the file name or the declared type.
_MAGIC = (
    (b"\xff\xd8\xff", KIND_JPEG),
    (b"\x89PNG\r\n\x1a\n", KIND_PNG),
    (b"%PDF-", KIND_PDF),
)


class Rejected(Exception):
    """The upload is refused before anything is stored or sent anywhere.

    ``code`` is a defect/telemetry code; ``message`` is what the customer
    reads. Neither ever contains the file's content.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def kind_of(data):
    """``jpeg`` / ``png`` / ``webp`` / ``heic`` / ``pdf`` from the bytes, or None."""
    head = data[:16]
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return KIND_WEBP
    if head[4:8] == b"ftyp":
        return KIND_HEIC
    return None


def _pillow():
    from PIL import Image, ImageOps  # noqa: PLC0415 - optional at import
    return Image, ImageOps


def _image_to_jpeg(image):
    Image, ImageOps = _pillow()
    image = ImageOps.exif_transpose(image)
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    w, h = image.size
    scale = min(1.0, MAX_SIDE_PX / float(max(w, h)))
    if scale < 1.0:
        image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                             Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def _raster_pdf(data):
    """The first pages of a PDF as JPEG bytes, or a refusal.

    PyMuPDF is optional: a host without it refuses PDFs with a message that
    tells the customer what to do instead (photograph the paper), rather than
    failing later in a way nobody can act on.
    """
    try:
        import pymupdf  # noqa: PLC0415
    except ImportError:
        try:
            import fitz as pymupdf  # noqa: PLC0415
        except ImportError:
            raise Rejected("RX_UPLOAD_PDF_UNSUPPORTED",
                           "PDF files cannot be read here yet. Please upload "
                           "a photo of your prescription instead.")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:  # noqa: BLE001 - any parser failure is one refusal
        raise Rejected("RX_UPLOAD_REJECTED",
                       "That PDF could not be opened. Please upload a photo "
                       "of your prescription instead.")
    if doc.is_encrypted:
        raise Rejected("RX_UPLOAD_REJECTED",
                       "That PDF is password-protected. Please upload a photo "
                       "of your prescription instead.")
    pages = []
    Image, _ = _pillow()
    for index in range(min(doc.page_count, MAX_PDF_PAGES)):
        pix = doc[index].get_pixmap(dpi=PDF_RENDER_DPI, alpha=False)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        pages.append(_image_to_jpeg(image))
    if not pages:
        raise Rejected("RX_UPLOAD_REJECTED", "That PDF has no pages.")
    return pages


def normalise(data):
    """``{"kind", "sha256", "pages": [jpeg bytes, ...]}`` for an accepted upload.

    The stored artefact is always JPEG this application encoded: the
    orientation is applied, the longest side is bounded, metadata is dropped.
    A PDF becomes one JPEG per page (bounded). Anything else is ``Rejected``.
    """
    size = len(data or b"")
    if size < MIN_UPLOAD_BYTES:
        raise Rejected("RX_UPLOAD_REJECTED",
                       "That file is empty or too small to be a prescription.")
    if size > MAX_UPLOAD_BYTES:
        raise Rejected("RX_UPLOAD_REJECTED",
                       "That file is larger than 12 MB. Please upload a "
                       "smaller photo.")
    kind = kind_of(data)
    if kind is None:
        raise Rejected("RX_UPLOAD_REJECTED",
                       "Please upload a JPEG, PNG, WebP or PDF of your "
                       "prescription.")
    if kind == KIND_PDF:
        pages = _raster_pdf(data)
    else:
        Image, _ = _pillow()
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except Exception:  # noqa: BLE001 - a decoder failure is one refusal
            raise Rejected("RX_UPLOAD_REJECTED",
                           "That image could not be read. Please try another "
                           "photo of your prescription.")
        pages = [_image_to_jpeg(image)]
    return {"kind": kind, "sha256": hashlib.sha256(data).hexdigest(),
            "original_bytes": size, "pages": pages}


def stored_name(document_id, page=0):
    """The file name a page is kept under; nothing of the customer's in it."""
    return "cldoc-%d-%d.jpg" % (int(document_id), int(page))


# --------------------------------------------------------------------------
# the reading
# --------------------------------------------------------------------------

DEFAULT_PROVIDER = "openai_vision"
PROVIDERS = ("openai_vision", "deepseek_vision")

# One question, asked of every provider in the same words; one answer shape,
# parsed by the same code. The model is told what the lens can be made in so
# that it transcribes into those terms (e.g. "PWR" as sph), but it is not told
# to pick a value — a reading the matrix does not hold is refused afterwards.
PROMPT = (
    "You are reading a photograph or scan of a contact lens or spectacle "
    "prescription for the customer who owns it. Transcribe only what is "
    "written; do not guess or infer missing values.\n"
    "Return ONLY a JSON object, no prose, of the form:\n"
    '{"right": {"sph": "-3.75", "cyl": "-0.75", "axis": "180", "add": "", '
    '"bc": "8.6"}, "left": {...}, "confidence": 0.0-1.0, '
    '"unreadable": false, "notes": "short"}\n'
    "Rules: 'right' is OD/R/RE, 'left' is OS/L/LE. Use sph for SPH, PWR, "
    "POWER or SPHERE. Use signed decimals with two places (\"-0.50\", "
    "\"+1.25\"); 'plano' or 'PL' is \"0.00\". Leave a field \"\" when it is "
    "not written. Omit an eye entirely if nothing is written for it. Set "
    "unreadable=true when the document is not a prescription or cannot be "
    "read. Never include the patient's name or any other personal detail."
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def provider_name(configured):
    """The workload to ask, defaulting when the setting is unset or unknown."""
    name = (configured or "").strip().lower()
    return name if name in PROVIDERS else DEFAULT_PROVIDER


def messages_for(pages_jpeg):
    """The chat-completion messages for the pages: prompt + inline images."""
    import base64  # noqa: PLC0415
    content = [{"type": "text", "text": PROMPT}]
    for page in pages_jpeg:
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," +
                   base64.b64encode(page).decode("ascii"),
            "detail": "high"}})
    return [{"role": "user", "content": content}]


def parse_reading(text):
    """``(proposal_or_None, confidence, unreadable)`` from the model's answer.

    The proposal is in ``lens_rx.extract_proposal``'s shape — the same one an
    Ask-AI reading arrives in — so everything downstream (validation,
    pre-fill, provenance) is the code the other doors already use.
    """
    cleaned = _FENCE.sub("", (text or "").strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None, None, True
    try:
        raw = json.loads(cleaned[start:end + 1])
    except ValueError:
        return None, None, True
    if not isinstance(raw, dict):
        return None, None, True
    confidence = raw.get("confidence")
    try:
        confidence = round(min(1.0, max(0.0, float(confidence))), 2)
    except (TypeError, ValueError):
        confidence = None
    if raw.get("unreadable") is True:
        return None, confidence, True
    proposal = lens_rx.proposal_from_mapping(
        {eye: raw.get(eye) for eye in lens_order.EYES})
    return proposal, confidence, proposal is None


def parsed_payload(proposal, confidence):
    """What is stored about the reading: typed values for the page, only."""
    return json.dumps({"eyes": proposal, "confidence": confidence},
                      sort_keys=True)


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------

def retain_until(now):
    return lens_rx.retain_until(now)


def insert(cursor, customer_id, product_id, site, norm, now=None):
    """The document row for an accepted upload; returns ``document_id``."""
    now = now or datetime.datetime.now()
    cursor.execute(
        "INSERT INTO contact_lens_documents (customer_id, product_id, site, "
        "status, original_kind, original_bytes, sha256, pages, created_at, "
        "retain_until) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (int(customer_id), int(product_id), site, ST_UPLOADED, norm["kind"],
         norm["original_bytes"], norm["sha256"], len(norm["pages"]), now,
         retain_until(now)))
    return cursor.lastrowid


def mark_stored(cursor, document_id, name, stored_bytes):
    cursor.execute(
        "UPDATE contact_lens_documents SET stored_name=%s, stored_bytes=%s "
        "WHERE document_id=%s", (name, int(stored_bytes), int(document_id)))


def mark_parsed(cursor, document_id, provider, model, proposal, confidence,
                status, refusal=None, now=None):
    now = now or datetime.datetime.now()
    cursor.execute(
        "UPDATE contact_lens_documents SET status=%s, provider=%s, model=%s, "
        "parsed_json=%s, confidence=%s, refusal=%s, parsed_at=%s "
        "WHERE document_id=%s",
        (status, provider, model,
         parsed_payload(proposal, confidence) if proposal else None,
         confidence, (refusal or None) and str(refusal)[:255], now,
         int(document_id)))


def mark_confirmed(cursor, document_id, customer_id, now=None):
    """The customer added the read values to the cart; only their own row."""
    now = now or datetime.datetime.now()
    cursor.execute(
        "UPDATE contact_lens_documents SET status=%s, confirmed_at=%s "
        "WHERE document_id=%s AND customer_id=%s AND purged_at IS NULL",
        (ST_CONFIRMED, now, int(document_id), int(customer_id)))
    return cursor.rowcount > 0


def by_id(cursor, document_id):
    cursor.execute("SELECT * FROM contact_lens_documents WHERE document_id=%s",
                   (int(document_id),))
    return cursor.fetchone()


def owned(cursor, customer_id, document_id):
    """The row only if it belongs to this customer; a stranger's id is None."""
    if not customer_id or not document_id:
        return None
    try:
        document_id = int(document_id)
    except (TypeError, ValueError):
        return None
    cursor.execute(
        "SELECT * FROM contact_lens_documents WHERE document_id=%s AND "
        "customer_id=%s AND purged_at IS NULL", (document_id, int(customer_id)))
    return cursor.fetchone()


def audit(cursor, document_id, action, actor=None, ip=None, detail=None,
          now=None):
    cursor.execute(
        "INSERT INTO contact_lens_document_audit (document_id, action, actor, "
        "ip, detail, occurred_at) VALUES (%s,%s,%s,%s,%s,%s)",
        (int(document_id), action, (actor or None) and str(actor)[:191],
         (ip or None) and str(ip)[:64], (detail or None) and str(detail)[:255],
         now or datetime.datetime.now()))


def set_ket_ref(cursor, document_id, ref):
    """Record the KET ticket a document was referred under. A stub: KET is
    told by the support flow, not from here; this is where the reference
    lands so Ops can go from document to ticket and back."""
    ref = (ref or "").strip()[:64]
    if not re.match(r"^[A-Za-z0-9._:-]{1,64}$", ref):
        return False
    cursor.execute(
        "UPDATE contact_lens_documents SET ket_ref=%s WHERE document_id=%s",
        (ref, int(document_id)))
    return cursor.rowcount > 0


def ket_reference(row):
    """What KET would be given about a document: identifiers and state, not
    the image and not the powers."""
    return {
        "type": "contact_lens_document",
        "document_id": int(row["document_id"]),
        "status": row.get("status"),
        "pages": int(row.get("pages") or 1),
        "created_at": (row["created_at"].isoformat(timespec="seconds")
                       if isinstance(row.get("created_at"), datetime.datetime)
                       else str(row.get("created_at") or "")),
        "ket_ref": row.get("ket_ref"),
    }


def ops_view(row):
    """The row for an Ops surface: everything but the values the customer
    read into the cards. Those are on the order, where Ops already see them."""
    keep = ("document_id", "customer_id", "product_id", "site", "status",
            "original_kind", "original_bytes", "stored_bytes", "pages",
            "provider", "model", "confidence", "refusal", "ket_ref",
            "created_at", "parsed_at", "confirmed_at", "retain_until",
            "purged_at")
    out = {}
    for key in keep:
        value = row.get(key)
        if isinstance(value, (datetime.datetime, datetime.date)):
            value = value.isoformat()
        elif value is not None and key in ("confidence",):
            value = float(value)
        out[key] = value
    return out


def due_for_purge(cursor, today=None):
    today = today or datetime.date.today()
    cursor.execute(
        "SELECT document_id, stored_name, pages FROM contact_lens_documents "
        "WHERE retain_until < %s AND purged_at IS NULL", (today,))
    return list(cursor.fetchall())


def mark_purged(cursor, document_id, now=None):
    cursor.execute(
        "UPDATE contact_lens_documents SET status=%s, purged_at=%s, "
        "stored_name=NULL, parsed_json=NULL WHERE document_id=%s",
        (ST_PURGED, now or datetime.datetime.now(), int(document_id)))
