"""
A customer's faces: the people a customer buys glasses for, each with their own
measurements.

One account, several named Face Profiles. Every customer has exactly one
``Self`` profile, which cannot be deleted or turned into somebody else, and
exactly one active default profile, which is the person the account is
currently shopping for. A face scan belongs to a profile, never directly to the
customer, so a household's measurements never blend.

The measurement engine is untouched: the browser measures exactly as before and
posts the same numbers. What changes is where they land — a ``face_scans`` row
owned by a profile — and that the customer's default profile is mirrored into
the legacy ``face_measurements`` row, so every surface that still reads that
table (product-page fit, matching frames, the assistant) keeps working on the
default person until each is taught about profiles.

Invariants are held by two nullable "slot" columns with unique keys, not by
application discipline alone: ``self_slot`` is 1 on the Self profile and NULL
elsewhere, so ``UNIQUE (customer_id, self_slot)`` refuses a second Self;
``default_slot`` does the same for the active default. The database is the
last line, the service the first.

Nothing here recognises a face. A profile is a label the customer typed, and a
scan is assigned to the label the customer chose before measuring.

Stdlib only at import: deploy/deploy.py reads TABLES from here for the
migration plan. Service functions take a DB-API connection with dict cursors.
"""

import os
import uuid
from datetime import datetime, timedelta

REL_SELF = "self"
RELATIONSHIPS = ("self", "spouse", "partner", "child", "parent", "sibling",
                 "friend", "other")
RELATIONSHIP_LABELS = {
    "self": "Me", "spouse": "Spouse", "partner": "Partner", "child": "Child",
    "parent": "Parent", "sibling": "Sibling", "friend": "Friend",
    "other": "Other",
}
NAME_MAX = 60

ST_STARTED = "STARTED"
ST_PROCESSING = "PROCESSING"
ST_PENDING_ASSIGNMENT = "PENDING_ASSIGNMENT"
ST_COMPLETED = "COMPLETED"
ST_FAILED = "FAILED"
ST_SUPERSEDED = "SUPERSEDED"
ST_EXPIRED = "EXPIRED"

SRC_TRYON = "tryon"
SRC_MIGRATED = "migrated"
SRC_STAFF = "staff_link"

# Owner-confirmed policy (2026-09-16): seven days is enough to recover a scan
# or debug one, and a face capture should not outlive its usefulness.
DEFAULT_PENDING_SCAN_RETENTION_DAYS = 7
DEFAULT_RAW_CAPTURE_RETENTION_DAYS = 7

# The same bounds the fitting endpoint enforces (ai_api.recommend_frame_fit).
FACE_WIDTH_MIN, FACE_WIDTH_MAX = 100.0, 180.0
PD_MIN, PD_MAX = 40.0, 80.0

PROFILES_SCHEMA = """
CREATE TABLE IF NOT EXISTS face_profiles (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    customer_id       INT NOT NULL,
    display_name      VARCHAR(60) NOT NULL,
    relationship_type VARCHAR(16) NOT NULL DEFAULT 'other',
    is_self           TINYINT(1) NOT NULL DEFAULT 0,
    is_default        TINYINT(1) NOT NULL DEFAULT 0,
    is_active         TINYINT(1) NOT NULL DEFAULT 1,
    self_slot         TINYINT NULL,
    default_slot      TINYINT NULL,
    latest_scan_id    BIGINT UNSIGNED NULL,
    consent_recorded_at DATETIME NULL,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                      ON UPDATE CURRENT_TIMESTAMP,
    deleted_at        DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_fp_self (customer_id, self_slot),
    UNIQUE KEY uq_fp_default (customer_id, default_slot),
    KEY idx_fp_customer (customer_id, is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SCANS_SCHEMA = """
CREATE TABLE IF NOT EXISTS face_scans (
    id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    customer_id          INT NOT NULL,
    face_profile_id      BIGINT UNSIGNED NULL,
    scan_group_id        CHAR(36) NULL,
    status               VARCHAR(20) NOT NULL,
    source               VARCHAR(16) NOT NULL DEFAULT 'tryon',
    pd_far               DECIMAL(5,2) NULL,
    pd_near              DECIMAL(5,2) NULL,
    face_width           DECIMAL(5,2) NULL,
    eye_mouth            DECIMAL(5,2) NULL,
    recommended_diameter INT NULL,
    recommended_bridge   INT NULL,
    recommended_length   INT NULL,
    decentration         DECIMAL(5,2) NULL,
    frame_candidates     TEXT NULL,
    capture_path         VARCHAR(255) NULL,
    capture_purged_at    DATETIME NULL,
    algorithm_version    VARCHAR(32) NULL,
    legacy_measurement_id INT NULL,
    failure_reason       VARCHAR(120) NULL,
    measured_at          DATETIME NULL,
    created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                         ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_fs_legacy (legacy_measurement_id),
    KEY idx_fs_profile (face_profile_id, status, measured_at),
    KEY idx_fs_customer (customer_id, status, created_at),
    KEY idx_fs_group (scan_group_id),
    KEY idx_fs_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (("face_profiles", PROFILES_SCHEMA), ("face_scans", SCANS_SCHEMA))

MEASUREMENT_FIELDS = ("pd_far", "pd_near", "face_width", "eye_mouth",
                      "recommended_diameter", "recommended_bridge",
                      "recommended_length", "decentration", "frame_candidates")

_SCHEMA_READY = False


class ProfileError(Exception):
    """A refused operation; ``code`` is stable for the API, ``status`` its HTTP."""

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class NotFound(ProfileError):
    """Absent or another customer's — indistinguishable on purpose."""

    def __init__(self):
        super().__init__("not_found", "Face profile not found", 404)


def ensure_schema(db):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    cur = db.cursor()
    for _name, ddl in TABLES:
        cur.execute(ddl)
    db.commit()
    _SCHEMA_READY = True


# --------------------------------------------------------------------------
# feature gate
# --------------------------------------------------------------------------

def enabled_for(email, enabled, allow_emails):
    """Stage-1 gate: the flag on, and the account on the allow-list.

    An empty allow-list with the flag on means every customer — that is the
    general-release setting, reached only by widening the list to nothing.
    """
    if not _truthy(enabled):
        return False
    allow = {e.strip().lower() for e in (allow_emails or "").split(",")
             if e.strip()}
    if not allow:
        return True
    return (email or "").strip().lower() in allow


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def clean_name(name):
    name = " ".join(str(name or "").split())
    if not name:
        raise ProfileError("name_required", "Give this person a name")
    if len(name) > NAME_MAX:
        raise ProfileError("name_too_long",
                           "Name must be %d characters or fewer" % NAME_MAX)
    return name


def clean_relationship(rel, allow_self=False):
    rel = str(rel or "other").strip().lower()
    if rel not in RELATIONSHIPS or (rel == REL_SELF and not allow_self):
        raise ProfileError("bad_relationship", "Unknown relationship")
    return rel


def _num(value, lo, hi, field):
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ProfileError("bad_measurement", "%s is not a number" % field)
    if not (lo <= v <= hi):
        raise ProfileError("bad_measurement",
                           "%s %.2f is outside %.0f-%.0f mm" % (field, v, lo, hi))
    return round(v, 2)


def clean_measurements(data):
    """The numbers the browser measured, bounded the way the fit engine is."""
    out = {
        "pd_far": _num(data.get("pd_far"), PD_MIN, PD_MAX, "Far PD"),
        "pd_near": _num(data.get("pd_near"), PD_MIN, PD_MAX, "Near PD"),
        "face_width": _num(data.get("face_width"), FACE_WIDTH_MIN,
                           FACE_WIDTH_MAX, "Face width"),
        "eye_mouth": _num(data.get("eye_mouth"), 0, 200, "Eye-mouth"),
        "decentration": _num(data.get("decentration"), -50, 50, "Decentration"),
    }
    for key in ("recommended_diameter", "recommended_bridge",
                "recommended_length"):
        v = data.get(key)
        try:
            out[key] = int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            raise ProfileError("bad_measurement", "%s is not a number" % key)
    if out["pd_far"] is None or out["face_width"] is None:
        raise ProfileError("bad_measurement",
                           "Far PD and face width are required")
    return out


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def _profile_row(cur, customer_id, profile_id, include_deleted=False):
    sql = ("SELECT * FROM face_profiles WHERE id=%s AND customer_id=%s")
    if not include_deleted:
        sql += " AND is_active=1"
    cur.execute(sql, (int(profile_id), int(customer_id)))
    return cur.fetchone()


def require_profile(db, customer_id, profile_id):
    """The customer's own active profile, or 404. Never 403: an id that is
    not yours does not exist as far as you can tell."""
    try:
        pid = int(profile_id)
    except (TypeError, ValueError):
        raise NotFound()
    row = _profile_row(db.cursor(), customer_id, pid)
    if not row:
        raise NotFound()
    return row


def list_profiles(db, customer_id):
    """Active profiles with their latest completed scan, Self first, then
    default, then by creation."""
    cur = db.cursor()
    cur.execute(
        "SELECT p.*, s.pd_far, s.pd_near, s.face_width, s.eye_mouth, "
        "s.recommended_diameter, s.recommended_bridge, s.recommended_length, "
        "s.decentration, s.frame_candidates, s.measured_at, s.capture_path, "
        "s.capture_purged_at, s.source AS scan_source "
        "FROM face_profiles p "
        "LEFT JOIN face_scans s ON s.id = p.latest_scan_id "
        "WHERE p.customer_id=%s AND p.is_active=1 "
        "ORDER BY p.is_self DESC, p.is_default DESC, p.created_at, p.id",
        (int(customer_id),))
    return [dict(r) for r in cur.fetchall()]


def get_profile(db, customer_id, profile_id):
    for row in list_profiles(db, customer_id):
        if int(row["id"]) == int(profile_id):
            return row
    raise NotFound()


def default_profile(db, customer_id):
    for row in list_profiles(db, customer_id):
        if row.get("is_default"):
            return row
    return None


def self_profile(db, customer_id):
    for row in list_profiles(db, customer_id):
        if row.get("is_self"):
            return row
    return None


def public_view(row, capture_url=None):
    """What the browser is shown. Never the capture path on disk."""
    m = None
    if row.get("pd_far") is not None:
        m = {k: _jsonable(row.get(k)) for k in MEASUREMENT_FIELDS
             if k != "frame_candidates"}
        m["recommended_size"] = None
        if row.get("recommended_diameter") and row.get("recommended_bridge") \
                and row.get("recommended_length"):
            m["recommended_size"] = "%s-%s-%s" % (
                row["recommended_diameter"], row["recommended_bridge"],
                row["recommended_length"])
        m["measured_at"] = _jsonable(row.get("measured_at"))
    return {
        "id": int(row["id"]),
        "display_name": row["display_name"],
        "relationship_type": row["relationship_type"],
        "relationship_label": RELATIONSHIP_LABELS.get(
            row["relationship_type"], "Other"),
        "is_self": bool(row.get("is_self")),
        "is_default": bool(row.get("is_default")),
        "has_scan": m is not None,
        "measurements": m,
        "capture_url": capture_url if (m is not None and row.get("capture_path")
                                       and not row.get("capture_purged_at")) else None,
        "created_at": _jsonable(row.get("created_at")),
    }


def _jsonable(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%dT%H:%M:%S")
    if v is None:
        return None
    if isinstance(v, (int, str, bool)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------

def ensure_self(db, customer_id, account_name=None):
    """The customer's Self profile, created as the default if absent.

    Idempotent under concurrency: a race to create two loses on
    ``uq_fp_self`` and re-reads.
    """
    row = self_profile(db, customer_id)
    if row:
        return row
    name = " ".join(str(account_name or "").split())[:NAME_MAX] or "Me"
    cur = db.cursor()
    has_default = default_profile(db, customer_id) is not None
    try:
        cur.execute(
            "INSERT INTO face_profiles (customer_id, display_name, "
            "relationship_type, is_self, is_default, is_active, self_slot, "
            "default_slot, consent_recorded_at) VALUES (%s,%s,'self',1,%s,1,1,%s,NOW())",
            (int(customer_id), name, 0 if has_default else 1,
             None if has_default else 1))
        db.commit()
    except Exception:  # noqa: BLE001 - duplicate on the unique slot
        db.rollback()
    row = self_profile(db, customer_id)
    if not row:
        raise ProfileError("self_missing", "Could not create the Self profile", 500)
    return row


def create_profile(db, customer_id, display_name, relationship_type,
                   consent=False, account_name=None):
    """A new person. Saving somebody else's measurements needs their consent,
    asserted by the customer; the time of that assertion is recorded."""
    name = clean_name(display_name)
    rel = clean_relationship(relationship_type)
    if not consent:
        raise ProfileError("consent_required",
                           "Confirm you have this person's permission to save "
                           "their measurements")
    ensure_self(db, customer_id, account_name)
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM face_profiles WHERE customer_id=%s "
                "AND is_active=1", (int(customer_id),))
    if int(cur.fetchone()["n"]) >= 12:
        raise ProfileError("too_many", "Up to 12 people per account", 409)
    cur.execute(
        "INSERT INTO face_profiles (customer_id, display_name, relationship_type, "
        "is_self, is_default, is_active, consent_recorded_at) "
        "VALUES (%s,%s,%s,0,0,1,NOW())", (int(customer_id), name, rel))
    pid = cur.lastrowid
    db.commit()
    return _profile_row(cur, customer_id, pid)


def rename_profile(db, customer_id, profile_id, display_name=None,
                   relationship_type=None):
    row = require_profile(db, customer_id, profile_id)
    sets, args = [], []
    if display_name is not None:
        sets.append("display_name=%s")
        args.append(clean_name(display_name))
    if relationship_type is not None:
        rel = clean_relationship(relationship_type, allow_self=bool(row["is_self"]))
        if row["is_self"] and rel != REL_SELF:
            raise ProfileError("self_immutable",
                               "Your own profile stays yours", 409)
        if not row["is_self"] and rel == REL_SELF:
            raise ProfileError("self_immutable",
                               "Only one profile can be you", 409)
        sets.append("relationship_type=%s")
        args.append(rel)
    if not sets:
        return row
    cur = db.cursor()
    cur.execute("UPDATE face_profiles SET " + ", ".join(sets)
                + " WHERE id=%s AND customer_id=%s",
                tuple(args) + (int(row["id"]), int(customer_id)))
    db.commit()
    return _profile_row(cur, customer_id, row["id"])


def set_default(db, customer_id, profile_id):
    """Exactly one default: the old one is cleared and the new one set in one
    transaction, in that order, so ``uq_fp_default`` never sees two."""
    row = require_profile(db, customer_id, profile_id)
    cur = db.cursor()
    try:
        cur.execute("UPDATE face_profiles SET is_default=0, default_slot=NULL "
                    "WHERE customer_id=%s AND default_slot=1 AND id<>%s",
                    (int(customer_id), int(row["id"])))
        cur.execute("UPDATE face_profiles SET is_default=1, default_slot=1 "
                    "WHERE id=%s AND customer_id=%s AND is_active=1",
                    (int(row["id"]), int(customer_id)))
        _mirror_default(cur, customer_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _profile_row(cur, customer_id, row["id"])


def delete_profile(db, customer_id, profile_id):
    """Soft-delete a non-Self profile. If it was the default, Self becomes the
    default in the same transaction. Live references (cart lines, favourites)
    are the caller's to resolve first — see ``references``."""
    row = require_profile(db, customer_id, profile_id)
    if row["is_self"]:
        raise ProfileError("self_protected",
                           "Your own profile cannot be deleted", 409)
    cur = db.cursor()
    try:
        cur.execute("UPDATE face_profiles SET is_active=0, deleted_at=NOW(), "
                    "is_default=0, default_slot=NULL WHERE id=%s AND customer_id=%s",
                    (int(row["id"]), int(customer_id)))
        if row["is_default"]:
            me = self_profile(db, customer_id)
            if me:
                cur.execute("UPDATE face_profiles SET is_default=1, default_slot=1 "
                            "WHERE id=%s", (int(me["id"]),))
        cur.execute("UPDATE face_scans SET status=%s WHERE face_profile_id=%s "
                    "AND status=%s", (ST_SUPERSEDED, int(row["id"]), ST_COMPLETED))
        _mirror_default(cur, customer_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"deleted": int(row["id"]),
            "default_restored_to_self": bool(row["is_default"])}


def references(db, customer_id, profile_id):
    """Live objects pointing at a profile. Empty until cart lines and
    favourites carry a person (later release); the shape is fixed now so the
    UI's confirmation dialog does not change."""
    require_profile(db, customer_id, profile_id)
    return {"cart_items": 0, "favorites": 0, "tryon_sessions": 0}


# --------------------------------------------------------------------------
# scans
# --------------------------------------------------------------------------

def record_scan(db, customer_id, profile_id, measurements, source=SRC_TRYON,
                capture_path=None, algorithm_version=None, scan_group_id=None):
    """A completed measurement for one profile.

    The previous completed scan of that profile is superseded, the profile's
    ``latest_scan_id`` moves, and — if this profile is the default — the
    legacy ``face_measurements`` row is rewritten so older surfaces keep
    reading the person the account is shopping for.
    """
    row = require_profile(db, customer_id, profile_id)
    m = clean_measurements(measurements)
    cur = db.cursor()
    try:
        cur.execute("UPDATE face_scans SET status=%s WHERE face_profile_id=%s "
                    "AND status=%s", (ST_SUPERSEDED, int(row["id"]), ST_COMPLETED))
        cur.execute(
            "INSERT INTO face_scans (customer_id, face_profile_id, scan_group_id, "
            "status, source, pd_far, pd_near, face_width, eye_mouth, "
            "recommended_diameter, recommended_bridge, recommended_length, "
            "decentration, frame_candidates, capture_path, algorithm_version, "
            "measured_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
            (int(customer_id), int(row["id"]), scan_group_id, ST_COMPLETED,
             source, m["pd_far"], m["pd_near"], m["face_width"], m["eye_mouth"],
             m["recommended_diameter"], m["recommended_bridge"],
             m["recommended_length"], m["decentration"],
             measurements.get("frame_candidates_json"), capture_path,
             algorithm_version))
        sid = cur.lastrowid
        cur.execute("UPDATE face_profiles SET latest_scan_id=%s WHERE id=%s",
                    (sid, int(row["id"])))
        if row["default_slot"]:
            _mirror_default(cur, customer_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return sid


def _mirror_default(cur, customer_id):
    """Rewrite the legacy single row from the default profile's latest scan.

    Column-for-column what ``/api/tryon/save`` used to write, so a reader of
    ``face_measurements`` cannot tell the difference — except that the row
    now follows the default person instead of the last person scanned.
    ``screenshot_path`` is left NULL: the capture lives outside the web root
    now and nothing may build a public URL to it.
    """
    cur.execute(
        "SELECT s.* FROM face_profiles p JOIN face_scans s ON s.id=p.latest_scan_id "
        "WHERE p.customer_id=%s AND p.default_slot=1 AND s.status=%s",
        (int(customer_id), ST_COMPLETED))
    s = cur.fetchone()
    cur.execute("DELETE FROM face_measurements WHERE customer_id=%s",
                (int(customer_id),))
    if not s:
        return
    cur.execute(
        "INSERT INTO face_measurements (customer_id, pd_far, pd_near, face_width, "
        "eye_mouth, recommended_diameter, recommended_bridge, recommended_length, "
        "decentration, frame_candidates, screenshot_path, measured_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s)",
        (int(customer_id), s["pd_far"], s["pd_near"], s["face_width"],
         s["eye_mouth"], s["recommended_diameter"], s["recommended_bridge"],
         s["recommended_length"], s["decentration"], s["frame_candidates"],
         s["measured_at"]))


# --------------------------------------------------------------------------
# migration from the single-face model
# --------------------------------------------------------------------------

def migrate_customer(db, customer_id, account_name=None):
    """Bring one customer onto profiles without rescanning.

    Their ``face_measurements`` row (there is at most one; the old save
    deleted before inserting) becomes the Self profile's completed scan,
    value for value, keyed on the legacy row id so running this twice — or
    from two workers — inserts once. Customers without a row get a Self
    profile with no scan, which is the "no scan yet" state.
    """
    cur = db.cursor()
    me = ensure_self(db, customer_id, account_name)
    cur.execute("SELECT * FROM face_measurements WHERE customer_id=%s "
                "ORDER BY measured_at DESC, id DESC", (int(customer_id),))
    rows = cur.fetchall()
    if not rows:
        return {"profile_id": int(me["id"]), "migrated": 0}
    migrated = 0
    for i, m in enumerate(rows):
        cur.execute("SELECT id FROM face_scans WHERE legacy_measurement_id=%s",
                    (int(m["id"]),))
        if cur.fetchone():
            continue
        status = ST_COMPLETED if i == 0 else ST_SUPERSEDED
        try:
            cur.execute(
                "INSERT INTO face_scans (customer_id, face_profile_id, status, "
                "source, pd_far, pd_near, face_width, eye_mouth, "
                "recommended_diameter, recommended_bridge, recommended_length, "
                "decentration, frame_candidates, capture_path, "
                "legacy_measurement_id, measured_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (int(customer_id), int(me["id"]), status, SRC_MIGRATED,
                 m["pd_far"], m["pd_near"], m["face_width"], m["eye_mouth"],
                 m["recommended_diameter"], m["recommended_bridge"],
                 m["recommended_length"], m["decentration"], m["frame_candidates"],
                 legacy_capture_name(m.get("screenshot_path")), int(m["id"]),
                 m["measured_at"]))
            sid = cur.lastrowid
            if status == ST_COMPLETED:
                cur.execute("UPDATE face_profiles SET latest_scan_id=%s WHERE id=%s "
                            "AND latest_scan_id IS NULL", (sid, int(me["id"])))
            db.commit()
            migrated += 1
        except Exception:  # noqa: BLE001 - lost the race on uq_fs_legacy
            db.rollback()
    return {"profile_id": int(me["id"]), "migrated": migrated}


def legacy_capture_name(screenshot_path):
    """``tryon/captures/face_1_ab12cd34.png`` -> ``face_1_ab12cd34.png``.

    The file itself is moved out of the web root by ``relocate_legacy_captures``;
    the scan row keeps only the bare name under the secure directory.
    """
    if not screenshot_path:
        return None
    return os.path.basename(str(screenshot_path)) or None


def _account_names(cur, customer_ids):
    """customer_id -> customer_name, or nothing where the customers table is
    not present (the isolated test database)."""
    if not customer_ids:
        return {}
    try:
        cur.execute("SELECT customer_id, customer_name FROM customers "
                    "WHERE customer_id IN (%s)"
                    % ",".join(["%s"] * len(customer_ids)), tuple(customer_ids))
    except Exception:  # noqa: BLE001 - table absent; the Self is named later
        return {}
    return {int(r["customer_id"]): r["customer_name"] for r in cur.fetchall()}


def migrate_all(db, dry_run=True):
    """Every customer who has a legacy measurement and no Self profile yet."""
    cur = db.cursor()
    cur.execute(
        "SELECT DISTINCT m.customer_id FROM face_measurements m "
        "LEFT JOIN face_profiles p ON p.customer_id=m.customer_id AND p.self_slot=1 "
        "WHERE p.id IS NULL ORDER BY m.customer_id")
    pending = cur.fetchall()
    names = _account_names(cur, [int(r["customer_id"]) for r in pending])
    if dry_run:
        return {"pending": [int(r["customer_id"]) for r in pending], "migrated": 0}
    done = 0
    for r in pending:
        migrate_customer(db, r["customer_id"], names.get(int(r["customer_id"])))
        done += 1
    return {"pending": [], "migrated": done}


def parity(db, customer_id):
    """The default profile's scan against the legacy row: equal or a list of
    the fields that differ. Used to prove a migration changed no number."""
    cur = db.cursor()
    cur.execute("SELECT * FROM face_measurements WHERE customer_id=%s "
                "ORDER BY measured_at DESC LIMIT 1", (int(customer_id),))
    legacy = cur.fetchone()
    d = default_profile(db, customer_id)
    scan = None
    if d and d.get("latest_scan_id"):
        cur.execute("SELECT * FROM face_scans WHERE id=%s", (d["latest_scan_id"],))
        scan = cur.fetchone()
    if not legacy and not scan:
        return []
    if not legacy or not scan:
        return ["presence"]
    diffs = []
    for k in MEASUREMENT_FIELDS:
        if _norm(legacy.get(k)) != _norm(scan.get(k)):
            diffs.append(k)
    return diffs


def _norm(v):
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return str(v)


# --------------------------------------------------------------------------
# captures: outside the web root, purged on schedule
# --------------------------------------------------------------------------

def capture_dir(app_root):
    """``<package parent>/secure_uploads/tryon/captures`` — a sibling of the
    package, never under ``static``."""
    path = os.path.join(os.path.dirname(app_root), "secure_uploads", "tryon",
                        "captures")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def capture_name(customer_id):
    """A name that carries nothing of the customer but the id, which the
    directory's owner-only permission already protects."""
    return "face_%d_%s.jpg" % (int(customer_id), uuid.uuid4().hex)


def store_capture(app_root, customer_id, data):
    name = capture_name(customer_id)
    path = os.path.join(capture_dir(app_root), name)
    with open(path, "wb") as fh:
        fh.write(data)
    os.chmod(path, 0o600)
    return name


def relocate_legacy_captures(app_root):
    """Move every file out of ``static/tryon/captures`` into the secure
    directory. Returns the names moved. Idempotent: an empty source dir is
    the finished state."""
    src = os.path.join(app_root, "static", "tryon", "captures")
    if not os.path.isdir(src):
        return []
    dst = capture_dir(app_root)
    moved = []
    for name in sorted(os.listdir(src)):
        s = os.path.join(src, name)
        if not os.path.isfile(s):
            continue
        d = os.path.join(dst, name)
        if os.path.exists(d):
            os.remove(s)
        else:
            os.replace(s, d)
            os.chmod(d, 0o600)
        moved.append(name)
    return moved


def purge_due_captures(db, app_root, retention_days, now=None):
    """Delete capture files older than the retention, and say so on the scan
    row. The measurements stay; only the photograph goes."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=int(retention_days))
    cur = db.cursor()
    cur.execute("SELECT id, capture_path FROM face_scans WHERE capture_path IS NOT NULL "
                "AND capture_purged_at IS NULL AND COALESCE(measured_at, created_at) < %s",
                (cutoff,))
    rows = cur.fetchall()
    directory = capture_dir(app_root)
    purged = []
    for r in rows:
        path = os.path.join(directory, os.path.basename(r["capture_path"]))
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            continue
        cur.execute("UPDATE face_scans SET capture_purged_at=%s WHERE id=%s",
                    (now, r["id"]))
        purged.append(int(r["id"]))
    # Files nobody's row names any more (a legacy capture whose row was
    # rewritten) are purged by age as well.
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and \
                    datetime.utcfromtimestamp(os.path.getmtime(path)) < cutoff:
                cur.execute("SELECT 1 FROM face_scans WHERE capture_path=%s "
                            "AND capture_purged_at IS NULL", (name,))
                if not cur.fetchone():
                    os.remove(path)
        except OSError:
            continue
    db.commit()
    return purged


def expire_pending_scans(db, retention_days, now=None):
    """A scan nobody assigned within the retention window is expired."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=int(retention_days))
    cur = db.cursor()
    cur.execute("UPDATE face_scans SET status=%s WHERE status=%s AND created_at < %s",
                (ST_EXPIRED, ST_PENDING_ASSIGNMENT, cutoff))
    n = cur.rowcount
    db.commit()
    return n


def run_retention(db, app_root, environ=None):
    """The daily job: expire unassigned scans and purge captures past their
    retention. Both windows come from the environment; the owner set seven
    days for each."""
    env = os.environ if environ is None else environ
    pending_days = int(env.get("FACE_PENDING_SCAN_RETENTION_DAYS",
                               DEFAULT_PENDING_SCAN_RETENTION_DAYS))
    capture_days = int(env.get("FACE_RAW_CAPTURE_RETENTION_DAYS",
                               DEFAULT_RAW_CAPTURE_RETENTION_DAYS))
    ensure_schema(db)
    expired = expire_pending_scans(db, pending_days)
    purged = purge_due_captures(db, app_root, capture_days)
    return {"expired_pending": expired, "purged_captures": len(purged),
            "pending_days": pending_days, "capture_days": capture_days}
