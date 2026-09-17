"""
Remote one-time face-scan requests: a customer asks somebody who is not in
the room to scan their own face for one named Face Profile.

The URL the recipient receives is a bearer credential for exactly one thing:
complete one scan for one profile of one customer. Everything about the
design follows from that.

* The token is random, hashed at rest, sent once, and named in no log.
  Operationally a request is its ``request_uuid``.
* Opening the link is not consuming it. Previews, scanners and a second tap
  all happen; the request is spent only when a valid scan is committed, or
  when it expires or the sender cancels it.
* One active request per profile, held by a unique slot, replaced
  transactionally; deleting the profile cancels it in the same breath.
* Request state and delivery state are separate columns. A WhatsApp that did
  not go out leaves a perfectly good request the sender can retry by email.
* Notifications go through the existing layer (MSG91 template, Flask-Mail),
  after commit, never from inside the transaction, and never carry a photo
  or a measurement.
* The scan itself is the same engine as the owner's: the guest page renders
  ``tryon.html`` against a token-authorised save route, and completion lands
  scan, ``latest_scan_id`` and request in one transaction.
"""
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta
from threading import Lock

from . import face_profiles as fp

TTL_ENV = "FACE_SCAN_REQUEST_TTL_HOURS"
ENABLED_ENV = "FACE_REMOTE_SCAN_ENABLED"
ALLOW_ENV = "FACE_REMOTE_SCAN_ALLOW_EMAILS"
LIMIT_PROFILE_ENV = "FACE_SCAN_REQUEST_MAX_PER_PROFILE_PER_DAY"
LIMIT_CUSTOMER_ENV = "FACE_SCAN_REQUEST_MAX_PER_CUSTOMER_PER_DAY"
LIMIT_DESTINATION_ENV = "FACE_SCAN_REQUEST_MAX_PER_DESTINATION_PER_DAY"
WA_TEMPLATE_ENV = "FACE_SCAN_REQUEST_WA_TEMPLATE"
MAIL_SENDER_ENV = "FACE_SCAN_REQUEST_MAIL_SENDER"

DEFAULT_TTL_HOURS = 24
DEFAULT_LIMIT_PROFILE = 3
DEFAULT_LIMIT_CUSTOMER = 10
DEFAULT_LIMIT_DESTINATION = 3
DEFAULT_WA_TEMPLATE = "face_scan_request"
DEFAULT_MAIL_SENDER = "Optiwar Support <support@optiwar.com>"
LINK_PATH = "/f/"
CONSENT_VERSION = "remote-scan-v1"

CH_WHATSAPP = "whatsapp"
CH_EMAIL = "email"
CHANNELS = (CH_WHATSAPP, CH_EMAIL)

ST_PENDING = "PENDING"
ST_OPENED = "OPENED"
ST_SCANNING = "SCANNING"
ST_COMPLETED = "COMPLETED"
ST_EXPIRED = "EXPIRED"
ST_CANCELLED = "CANCELLED"
ST_FAILED = "FAILED"
USABLE = (ST_PENDING, ST_OPENED, ST_SCANNING)

DL_NOT_SENT = "NOT_SENT"
DL_SENT = "SENT"
DL_FAILED = "FAILED"

EV_CREATED = "face.scan_request.created"
EV_SENT = "face.scan_request.sent"
EV_OPENED = "face.scan_request.opened"
EV_COMPLETED = "face.scan_request.completed"
EV_EXPIRED = "face.scan_request.expired"
EV_CANCELLED = "face.scan_request.cancelled"
EV_DELIVERY_FAILED = "face.scan_request.delivery_failed"

_EVENT_NS = uuid.UUID("6f1b2c9e-5a52-4d1f-9c7b-2d0f3e8a1b47")

INVITES_SCHEMA = """
CREATE TABLE IF NOT EXISTS face_scan_invites (
    id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    request_uuid       CHAR(36) NOT NULL,
    customer_id        INT NOT NULL,
    face_profile_id    BIGINT UNSIGNED NOT NULL,
    scan_group_id      CHAR(36) NULL,
    channel            VARCHAR(12) NOT NULL,
    recipient_email    VARCHAR(191) NULL,
    recipient_phone    VARCHAR(24) NULL,
    token_hash         CHAR(64) NOT NULL,
    status             VARCHAR(12) NOT NULL DEFAULT 'PENDING',
    active_slot        TINYINT NULL,
    sender_name        VARCHAR(120) NULL,
    site_host          VARCHAR(120) NULL,
    delivery_status    VARCHAR(12) NOT NULL DEFAULT 'NOT_SENT',
    delivery_error     VARCHAR(160) NULL,
    delivery_ref       VARCHAR(120) NULL,
    delivered_at       DATETIME NULL,
    delivery_attempts  INT NOT NULL DEFAULT 0,
    expires_at         DATETIME NOT NULL,
    opened_at          DATETIME NULL,
    consent_at         DATETIME NULL,
    consent_version    VARCHAR(32) NULL,
    scan_started_at    DATETIME NULL,
    completed_at       DATETIME NULL,
    cancelled_at       DATETIME NULL,
    completed_scan_id  BIGINT UNSIGNED NULL,
    created_ip         VARCHAR(45) NULL,
    completed_ip       VARCHAR(45) NULL,
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                       ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_fsi_uuid (request_uuid),
    UNIQUE KEY uq_fsi_token (token_hash),
    UNIQUE KEY uq_fsi_active (face_profile_id, active_slot),
    KEY idx_fsi_customer (customer_id, created_at),
    KEY idx_fsi_status (status, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS face_events (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    event_id         CHAR(36) NOT NULL,
    event_type       VARCHAR(48) NOT NULL,
    customer_id      INT NULL,
    face_profile_id  BIGINT UNSIGNED NULL,
    request_uuid     CHAR(36) NULL,
    scan_id          BIGINT UNSIGNED NULL,
    scan_group_id    CHAR(36) NULL,
    payload          TEXT NULL,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_fe_event (event_id),
    KEY idx_fe_type (event_type, created_at),
    KEY idx_fe_customer (customer_id, created_at),
    KEY idx_fe_request (request_uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (("face_scan_invites", INVITES_SCHEMA), ("face_events", EVENTS_SCHEMA))

_SCHEMA_READY = False


class InviteError(fp.ProfileError):
    pass


class InviteNotFound(InviteError):
    """Unknown, somebody else's, or a token that matches nothing: one answer."""

    def __init__(self):
        super().__init__("not_found", "Scan request not found", 404)


def ensure_schema(db):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    fp.ensure_schema(db)
    cur = db.cursor()
    for _name, ddl in TABLES:
        cur.execute(ddl)
    db.commit()
    _SCHEMA_READY = True


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def _env_int(environ, key, default):
    try:
        return max(0, int((environ or os.environ).get(key, default)))
    except (TypeError, ValueError):
        return default


def ttl_hours(environ=None):
    return _env_int(environ, TTL_ENV, DEFAULT_TTL_HOURS) or DEFAULT_TTL_HOURS


def limits(environ=None):
    return {
        "profile": _env_int(environ, LIMIT_PROFILE_ENV, DEFAULT_LIMIT_PROFILE),
        "customer": _env_int(environ, LIMIT_CUSTOMER_ENV, DEFAULT_LIMIT_CUSTOMER),
        "destination": _env_int(environ, LIMIT_DESTINATION_ENV,
                                DEFAULT_LIMIT_DESTINATION),
    }


def enabled_for(email, environ=None):
    """The remote-scan gate: its own flag, its own allow-list falling back to
    the Face Profiles one, and never on for an account the profile gate
    excludes."""
    env = os.environ if environ is None else environ
    if not fp.enabled_for(email, env.get("FACE_PROFILES_ENABLED"),
                          env.get("FACE_PROFILES_ALLOW_EMAILS")):
        return False
    allow = env.get(ALLOW_ENV) or env.get("FACE_PROFILES_ALLOW_EMAILS")
    return fp.enabled_for(email, env.get(ENABLED_ENV), allow)


# --------------------------------------------------------------------------
# destinations
# --------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def clean_channel(channel):
    ch = str(channel or "").strip().lower()
    if ch not in CHANNELS:
        raise InviteError("bad_channel", "Choose WhatsApp or email")
    return ch


def clean_email(value):
    v = str(value or "").strip().lower()
    if not v or len(v) > 191 or not _EMAIL_RE.match(v):
        raise InviteError("bad_email", "Enter a valid email address")
    return v


def clean_phone(value, default_cc="91"):
    """E.164 with the leading ``+``; the same rule the notification layer
    applies to customer numbers (ten digits default to India)."""
    raw = str(value or "").strip()
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not has_plus:
        digits = digits.lstrip("0")
        if len(digits) == 10:
            digits = default_cc + digits
    if not (8 <= len(digits) <= 15):
        raise InviteError("bad_phone", "Enter a valid WhatsApp number with country code")
    return "+" + digits


def clean_destination(channel, destination):
    if channel == CH_EMAIL:
        return clean_email(destination), None
    return None, clean_phone(destination)


def _same_contact(cleaner, candidate, owner_value):
    try:
        return bool(owner_value) and cleaner(owner_value) == candidate
    except InviteError:
        return False


def owner_contacts(db, customer_id, session_email=None):
    """The account holder's own reachable contacts: the customers row plus
    the authenticated email (they can differ on legacy accounts)."""
    cur = db.cursor()
    cur.execute("SELECT customer_email, customer_phone FROM customers WHERE customer_id=%s "
                "LIMIT 1", (int(customer_id),))
    row = cur.fetchone() or {}
    emails = [e for e in (row.get("customer_email"), session_email) if e]
    phones = [p for p in (row.get("customer_phone"),) if p]
    return {"emails": emails, "phones": phones}


def check_not_owner(email, phone, contacts):
    """A remote link is for another person's own destination: the account
    holder's phone or email, however formatted, is refused before anything is
    created or sent."""
    contacts = contacts or {}
    if phone and any(_same_contact(clean_phone, phone, p) for p in contacts.get("phones", ())):
        raise InviteError("own_phone", "This is your Optiwar account mobile number. Choose "
                          "Scan here or enter the other person's number.", 422)
    if email and any(_same_contact(clean_email, email, e) for e in contacts.get("emails", ())):
        raise InviteError("own_email", "This is your Optiwar account email. Choose Scan here "
                          "or enter the other person's email.", 422)


def mask_destination(row):
    if row.get("recipient_phone"):
        p = row["recipient_phone"]
        return p[:3] + "\u2022" * max(0, len(p) - 7) + p[-4:]
    e = row.get("recipient_email") or ""
    if "@" in e:
        local, dom = e.split("@", 1)
        return (local[:1] + "\u2022\u2022\u2022@" + dom)
    return ""


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------

def new_token():
    """192 random bits, 32 URL-safe characters: short enough to read out
    over the phone, far too long to guess."""
    return secrets.token_urlsafe(24)


def hash_token(token):
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def link_for(token, site_host):
    host = (site_host or "").strip()
    if host.startswith("http://") or host.startswith("https://"):
        base = host.rstrip("/")
    else:
        base = "https://" + host.rstrip("/")
    return "%s%s%s" % (base, LINK_PATH, token)


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

def event_id_for(event_type, request_uuid, suffix=""):
    """Deterministic per (type, request): a retry of the same transition
    produces the same id and the unique key makes it a no-op."""
    return str(uuid.uuid5(_EVENT_NS, "%s:%s:%s" % (event_type, request_uuid, suffix)))


def emit(db, event_type, row=None, payload=None, event_id=None, scan_id=None,
         commit=True):
    """Write one event; return True if it was new. Payload is labelled
    metadata only — never a token, a link, or a capture."""
    request_uuid = row.get("request_uuid") if row else None
    eid = event_id or (event_id_for(event_type, request_uuid) if request_uuid
                       else str(uuid.uuid4()))
    cur = db.cursor()
    cur.execute(
        "INSERT IGNORE INTO face_events (event_id, event_type, customer_id, "
        "face_profile_id, request_uuid, scan_id, scan_group_id, payload) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (eid, event_type,
         int(row["customer_id"]) if row else None,
         int(row["face_profile_id"]) if row and row.get("face_profile_id") is not None else None,
         request_uuid, scan_id,
         row.get("scan_group_id") if row else None,
         json.dumps(payload or {}, default=str)))
    new = cur.rowcount == 1
    if commit:
        db.commit()
    return new


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def _row_by(cur, where, args):
    cur.execute("SELECT * FROM face_scan_invites WHERE " + where, args)
    return cur.fetchone()


def by_uuid(db, request_uuid):
    return _row_by(db.cursor(), "request_uuid=%s", (str(request_uuid),))


def by_token(db, token):
    if not token or len(token) > 128:
        return None
    return _row_by(db.cursor(), "token_hash=%s", (hash_token(token),))


def for_profile(db, customer_id, profile_id):
    """The active request for the customer's own profile, else the most
    recent one — the card shows 'expired' after 'pending'."""
    fp.require_profile(db, customer_id, profile_id)
    cur = db.cursor()
    cur.execute("SELECT * FROM face_scan_invites WHERE customer_id=%s AND "
                "face_profile_id=%s ORDER BY active_slot DESC, created_at DESC, "
                "id DESC LIMIT 1", (int(customer_id), int(profile_id)))
    row = cur.fetchone()
    return refresh(db, row) if row else None


def for_customer(db, customer_id):
    """profile_id -> current request row, for the My Faces cards."""
    cur = db.cursor()
    cur.execute("SELECT * FROM face_scan_invites WHERE customer_id=%s "
                "ORDER BY face_profile_id, active_slot DESC, created_at DESC, id DESC",
                (int(customer_id),))
    out = {}
    for r in cur.fetchall():
        pid = int(r["face_profile_id"])
        if pid not in out:
            out[pid] = refresh(db, r)
    return out


def refresh(db, row):
    """A usable row past its expiry is expired on read, so no reader ever sees
    a live request the clock has already ended."""
    if row["status"] in USABLE and row["expires_at"] <= datetime.now():
        expire(db, row)
        row = by_uuid(db, row["request_uuid"])
    return row


def is_usable(row, now=None):
    return bool(row) and row["status"] in USABLE and \
        row["expires_at"] > (now or datetime.now())


def public_view(row, now=None):
    """The sender's view: lifecycle and delivery, masked destination, and
    nothing that could rebuild the link."""
    if not row:
        return None
    now = now or datetime.now()
    remaining = int((row["expires_at"] - now).total_seconds()) if row["expires_at"] else 0
    return {
        "request_uuid": row["request_uuid"],
        "status": row["status"],
        "channel": row["channel"],
        "destination": mask_destination(row),
        "delivery_status": row["delivery_status"],
        "delivery_error": row.get("delivery_error"),
        "expires_at": fp._jsonable(row["expires_at"]),
        "expires_in_seconds": max(0, remaining),
        "opened": row.get("opened_at") is not None,
        "consented": row.get("consent_at") is not None,
        "created_at": fp._jsonable(row["created_at"]),
        "completed_at": fp._jsonable(row.get("completed_at")),
        "active": is_usable(row, now),
    }


def guest_view(db, row):
    """What the recipient's page may know: who asked, which name, when it
    ends. No account details, no other profiles."""
    cur = db.cursor()
    cur.execute("SELECT display_name FROM face_profiles WHERE id=%s",
                (int(row["face_profile_id"]),))
    p = cur.fetchone()
    remaining = int((row["expires_at"] - datetime.now()).total_seconds())
    return {
        "sender_name": row.get("sender_name") or "An Optiwar customer",
        "profile_name": p["display_name"] if p else None,
        "expires_at": row["expires_at"],
        "expires_in_hours": max(1, (remaining + 3599) // 3600),
        "capture_retention_days": fp.DEFAULT_RAW_CAPTURE_RETENTION_DAYS,
        "status": row["status"],
        "consented": row.get("consent_at") is not None,
    }


# --------------------------------------------------------------------------
# rate limits
# --------------------------------------------------------------------------

def _count_day(cur, where, args):
    cur.execute("SELECT COUNT(*) AS n FROM face_scan_invites WHERE created_at > "
                "NOW() - INTERVAL 1 DAY AND " + where, args)
    return int(cur.fetchone()["n"])


def check_limits(db, customer_id, profile_id, email, phone, environ=None):
    lim = limits(environ)
    cur = db.cursor()
    if lim["customer"] and _count_day(cur, "customer_id=%s", (int(customer_id),)) >= lim["customer"]:
        raise InviteError("rate_limited", "Daily scan-request limit reached for your account", 429)
    if lim["profile"] and _count_day(cur, "face_profile_id=%s", (int(profile_id),)) >= lim["profile"]:
        raise InviteError("rate_limited", "Daily scan-request limit reached for this person", 429)
    if lim["destination"]:
        col, val = ("recipient_email", email) if email else ("recipient_phone", phone)
        if _count_day(cur, col + "=%s", (val,)) >= lim["destination"]:
            raise InviteError("rate_limited", "Daily scan-request limit reached for this number or address", 429)


class IpLimiter:
    """A small fixed-window counter for the guest routes. Per process — a
    brake on brute force, not an accounting system."""

    def __init__(self, max_hits=30, window_seconds=600):
        self.max_hits = max_hits
        self.window = window_seconds
        self._hits = {}
        self._lock = Lock()

    def allow(self, key, now=None):
        now = now or time.time()
        with self._lock:
            if len(self._hits) > 5000:
                self._hits = {k: v for k, v in self._hits.items()
                              if v[0] > now - self.window}
            start, n = self._hits.get(key, (now, 0))
            if start <= now - self.window:
                start, n = now, 0
            n += 1
            self._hits[key] = (start, n)
            return n <= self.max_hits


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

def create(db, customer_id, profile_id, channel, destination, sender_name=None,
           site_host=None, created_ip=None, scan_group_id=None, environ=None,
           now=None, contacts=None):
    """A new request for the customer's own, unscanned-or-not, non-Self
    profile; any active request for that profile is cancelled in the same
    transaction. Returns ``(row, token)`` — the only moment the token exists
    in plaintext on the server."""
    profile = fp.require_profile(db, customer_id, profile_id)
    if profile["is_self"]:
        raise InviteError("self_scan_here", "Scan your own face on this device", 409)
    ch = clean_channel(channel)
    email, phone = clean_destination(ch, destination)
    check_not_owner(email, phone, contacts)
    check_limits(db, customer_id, profile["id"], email, phone, environ)
    now = now or datetime.now()
    token = new_token()
    ruuid = str(uuid.uuid4())
    cur = db.cursor()
    try:
        cur.execute("UPDATE face_scan_invites SET status=%s, cancelled_at=%s, "
                    "active_slot=NULL WHERE face_profile_id=%s AND active_slot=1",
                    (ST_CANCELLED, now, int(profile["id"])))
        replaced = cur.rowcount
        cur.execute(
            "INSERT INTO face_scan_invites (request_uuid, customer_id, face_profile_id, "
            "scan_group_id, channel, recipient_email, recipient_phone, token_hash, "
            "status, active_slot, sender_name, site_host, expires_at, created_ip) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s)",
            (ruuid, int(customer_id), int(profile["id"]), scan_group_id, ch, email,
             phone, hash_token(token), ST_PENDING,
             (sender_name or "")[:120] or None, (site_host or "")[:120] or None,
             now + timedelta(hours=ttl_hours(environ)), (created_ip or "")[:45] or None))
        row = by_uuid(db, ruuid)
        emit(db, EV_CREATED, row, {"channel": ch, "replaced_active": replaced,
                                   "expires_at": str(row["expires_at"])},
             commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return row, token


def cancel(db, customer_id, request_uuid, reason="sender", now=None):
    row = by_uuid(db, request_uuid)
    if not row or int(row["customer_id"]) != int(customer_id):
        raise InviteNotFound()
    if row["status"] not in USABLE:
        return row
    now = now or datetime.now()
    cur = db.cursor()
    try:
        cur.execute("UPDATE face_scan_invites SET status=%s, cancelled_at=%s, "
                    "active_slot=NULL WHERE id=%s AND status IN %s",
                    (ST_CANCELLED, now, int(row["id"]), USABLE))
        if cur.rowcount:
            emit(db, EV_CANCELLED, row, {"reason": reason}, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return by_uuid(db, request_uuid)


def cancel_for_profile(db, customer_id, profile_id, reason="profile_deleted"):
    """Every live request for a profile — the hook profile deletion calls
    before the profile goes."""
    cur = db.cursor()
    cur.execute("SELECT request_uuid FROM face_scan_invites WHERE customer_id=%s "
                "AND face_profile_id=%s AND status IN %s",
                (int(customer_id), int(profile_id), USABLE))
    uuids = [r["request_uuid"] for r in cur.fetchall()]
    for u in uuids:
        cancel(db, customer_id, u, reason=reason)
    return len(uuids)


def expire(db, row, now=None):
    now = now or datetime.now()
    cur = db.cursor()
    try:
        cur.execute("UPDATE face_scan_invites SET status=%s, active_slot=NULL "
                    "WHERE id=%s AND status IN %s",
                    (ST_EXPIRED, int(row["id"]), USABLE))
        if cur.rowcount:
            emit(db, EV_EXPIRED, row, {"expires_at": str(row["expires_at"])},
                 commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise


def expire_due(db, now=None):
    """The scheduled sweep: every usable request past its clock."""
    now = now or datetime.now()
    cur = db.cursor()
    cur.execute("SELECT * FROM face_scan_invites WHERE status IN %s AND expires_at <= %s",
                (USABLE, now))
    rows = cur.fetchall()
    for r in rows:
        expire(db, r, now)
    return len(rows)


def mark_opened(db, row, now=None):
    """First verified open only; a refresh or a preview fetch after that
    changes nothing and emits nothing."""
    if row["status"] != ST_PENDING:
        return row
    now = now or datetime.now()
    cur = db.cursor()
    try:
        cur.execute("UPDATE face_scan_invites SET status=%s, opened_at=%s "
                    "WHERE id=%s AND status=%s",
                    (ST_OPENED, now, int(row["id"]), ST_PENDING))
        if cur.rowcount:
            emit(db, EV_OPENED, row, {}, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return by_uuid(db, row["request_uuid"])


def record_consent(db, row, now=None):
    if not is_usable(row, now):
        raise InviteError("inactive", "This scan request is no longer active", 410)
    now = now or datetime.now()
    cur = db.cursor()
    cur.execute("UPDATE face_scan_invites SET status=%s, consent_at=COALESCE(consent_at, %s), "
                "consent_version=%s, scan_started_at=COALESCE(scan_started_at, %s) "
                "WHERE id=%s AND status IN %s",
                (ST_SCANNING, now, CONSENT_VERSION, now, int(row["id"]), USABLE))
    db.commit()
    return by_uuid(db, row["request_uuid"])


def complete(db, row, measurements, capture_path=None, algorithm_version=None,
             completed_ip=None, now=None):
    """The one moment the token is spent: scan, profile pointer and request
    land together or not at all. Re-reads the row under the transaction so a
    cancellation that raced the submit wins."""
    now = now or datetime.now()
    cur = db.cursor()
    cur.execute("SELECT * FROM face_scan_invites WHERE id=%s FOR UPDATE", (int(row["id"]),))
    live = cur.fetchone()
    if not is_usable(live, now):
        db.rollback()
        if live and live["status"] == ST_COMPLETED:
            raise InviteError("completed", "This scan request is already complete", 410)
        if live and live["status"] in USABLE:
            expire(db, live, now)
            raise InviteError("expired", "This scan request has expired", 410)
        raise InviteError("inactive", "This scan request is no longer active", 410)
    if live.get("consent_at") is None:
        db.rollback()
        raise InviteError("consent_required", "Consent is required before scanning", 409)
    try:
        sid = fp.record_scan(db, live["customer_id"], live["face_profile_id"],
                             measurements, source=fp.SRC_REMOTE,
                             capture_path=capture_path,
                             algorithm_version=algorithm_version,
                             scan_group_id=live.get("scan_group_id"), commit=False)
        cur.execute("UPDATE face_scan_invites SET status=%s, completed_at=%s, "
                    "completed_scan_id=%s, completed_ip=%s, active_slot=NULL "
                    "WHERE id=%s AND status IN %s",
                    (ST_COMPLETED, now, sid, (completed_ip or "")[:45] or None,
                     int(live["id"]), USABLE))
        if cur.rowcount != 1:
            raise InviteError("inactive", "This scan request is no longer active", 410)
        m = fp.clean_measurements(measurements)
        emit(db, EV_COMPLETED, live,
             {"scan_id": sid, "pd_far": m["pd_far"], "pd_near": m["pd_near"],
              "face_width": m["face_width"],
              "recommended_size": "%s-%s-%s" % (m["recommended_diameter"],
                                                m["recommended_bridge"],
                                                m["recommended_length"])},
             scan_id=sid, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return sid


# --------------------------------------------------------------------------
# delivery (after commit; never inside a transaction)
# --------------------------------------------------------------------------

def record_delivery(db, row, ok, ref=None, error=None, now=None):
    now = now or datetime.now()
    cur = db.cursor()
    try:
        cur.execute("UPDATE face_scan_invites SET delivery_status=%s, delivery_ref=%s, "
                    "delivery_error=%s, delivered_at=%s, delivery_attempts=delivery_attempts+1 "
                    "WHERE id=%s",
                    (DL_SENT if ok else DL_FAILED, (ref or "")[:120] or None,
                     (error or "")[:160] or None, now if ok else None, int(row["id"])))
        emit(db, EV_SENT if ok else EV_DELIVERY_FAILED, row,
             {"channel": row["channel"], "error": error, "ref": ref},
             event_id=event_id_for(EV_SENT if ok else EV_DELIVERY_FAILED,
                                   row["request_uuid"],
                                   str(int(row["delivery_attempts"]) + 1)),
             commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return by_uuid(db, row["request_uuid"])


# The text submitted to MSG91/Meta as template ``face_scan_request`` (Utility,
# en). The send passes {{1}} = sender name and {{2}} = the link; the body
# itself lives with the provider (MSG91 template_id 3899902573485197), this copy
# is the record of what was submitted. Meta refuses a body that starts with a
# variable, hence the "Hello," line.
WA_TEMPLATE_HEADER = "Optiwar Face Scan Request"
WA_TEMPLATE_BODY = (
    "Hello,\n\n"
    "{{1}} has invited you to complete a quick face measurement for eyewear "
    "fitting on Optiwar.\n\n"
    "No Optiwar login is required.\n\n"
    "Open the secure link below to complete your face scan:\n\n"
    "{{2}}\n\n"
    "This link expires in 24 hours and can be used only for this face scan request.\n\n"
    "Please open the link only if you recognise the sender and were expecting this request.\n\n"
    "Safety notice: No payment is required to complete this face scan. Optiwar will "
    "never ask you to make a payment, share card details, OTPs, passwords, or banking "
    "credentials as part of a face scan request."
)

EMAIL_SUBJECT = "Optiwar \u2014 Face measurement request from {sender}"

SAFETY_NOTICE = ("Safety notice: No payment is required to complete this face scan. "
                 "Optiwar will never ask you to make a payment, share card details, "
                 "OTPs, passwords, or banking credentials as part of a face scan request.")

EMAIL_TEXT = """Hello,

{sender} has invited you to complete a face measurement for eyewear fitting on Optiwar.

You do not need an Optiwar account or login to complete the scan.

Please use the secure link below:

Complete Face Scan: {link}

The link is valid for 24 hours and can be used only for this face scan request.

Before the scan begins, Optiwar will ask for your consent and permission to use your camera. Your measurements will be saved only to the Face Profile for which this request was created.

If you were not expecting this request or do not recognise the sender, simply ignore this email.

""" + SAFETY_NOTICE + """

Regards,
Optiwar Support
Factory Outlet Opticals
support@optiwar.com
"""

EMAIL_HTML = """<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:auto;color:#222">
<p style="font-size:18px;font-weight:bold;letter-spacing:.5px">OPTIWAR</p>
<p>Hello,</p>
<p>{sender} has invited you to complete a face measurement for eyewear fitting on Optiwar.</p>
<p>You do not need an Optiwar account or login to complete the scan.</p>
<p>Please use the secure link below:</p>
<p style="margin:24px 0 8px"><a href="{link}" style="background:#5b3df5;color:#fff;\
text-decoration:none;padding:12px 22px;border-radius:8px;display:inline-block">Complete Face Scan</a></p>
<p style="margin:0 0 24px;font-size:13px"><code>{link}</code></p>
<p>The link is valid for 24 hours and can be used only for this face scan request.</p>
<p>Before the scan begins, Optiwar will ask for your consent and permission to use your camera. \
Your measurements will be saved only to the Face Profile for which this request was created.</p>
<p>If you were not expecting this request or do not recognise the sender, simply ignore this email.</p>
<p style="color:#666;font-size:13px">""" + SAFETY_NOTICE + """</p>
<p>Regards,<br>Optiwar Support<br>Factory Outlet Opticals<br>\
<a href="mailto:support@optiwar.com">support@optiwar.com</a></p>
</div>"""


def send(db, row, token, mailer=None, whatsapp=None, environ=None):
    """Deliver the invitation on the request's channel and record the
    outcome. The request itself is unaffected by a failed delivery."""
    env = os.environ if environ is None else environ
    link = link_for(token, row.get("site_host"))
    sender = row.get("sender_name") or "An Optiwar customer"
    if row["channel"] == CH_WHATSAPP:
        wa = whatsapp or _default_whatsapp
        template = env.get(WA_TEMPLATE_ENV, DEFAULT_WA_TEMPLATE)
        try:
            result = wa(row["recipient_phone"].lstrip("+"), template, {
                "body_1": {"type": "text", "value": sender},
                "body_2": {"type": "text", "value": link},
            }) or {}
        except Exception as exc:  # noqa: BLE001 - provider failure is a recorded state
            result = {"ok": False, "error": type(exc).__name__}
        ok = bool(result.get("ok"))
        return record_delivery(db, row, ok, ref=result.get("request_id"),
                               error=None if ok else (result.get("error") or "send_failed"))
    mail = mailer or _default_mailer
    try:
        mail(row["recipient_email"], EMAIL_SUBJECT.format(sender=sender),
             EMAIL_HTML.format(sender=_html(sender), link=link),
             EMAIL_TEXT.format(sender=sender, link=link),
             sender=env.get(MAIL_SENDER_ENV, DEFAULT_MAIL_SENDER))
        return record_delivery(db, row, True)
    except Exception as exc:  # noqa: BLE001 - provider failure is a recorded state
        return record_delivery(db, row, False, error=type(exc).__name__)


def _html(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _default_whatsapp(phone, template, components):
    from .notifications import send_whatsapp_tracked
    return send_whatsapp_tracked(phone, template, components)


def _default_mailer(to_email, subject, html, text, sender=DEFAULT_MAIL_SENDER):
    """Flask-Mail directly, with no BCC: the standard notification path copies
    the admin mailbox, and a bearer link must reach one inbox only."""
    from flask import current_app
    from flask_mail import Message
    msg = Message(subject=subject, recipients=[to_email], html=html, body=text,
                  sender=sender, reply_to="support@optiwar.com")
    current_app.extensions["mail"].send(msg)


# --------------------------------------------------------------------------
# retention hook
# --------------------------------------------------------------------------

def run_retention(db):
    ensure_schema(db)
    return {"expired_requests": expire_due(db)}
