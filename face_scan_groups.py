"""A scan group: several named faces the customer wants measured as one job.

A group is a set of the customer's Face Profiles ("members"), each waiting
for one completed scan. Members can be added and removed while the group is
open; each member's scan arrives either from the owner's own try-on page
(``record_member_scan``) or through a remote invitation created *for that
group* (``face_scan_invites.create(..., scan_group_id=...)``), so the scan
row carries ``scan_group_id`` and ``on_scan_completed`` knows which group
it belongs to. An ordinary scan with no group id never touches a group.

Completion is decided by **distinct members**, never by counting scans: the
group is complete when every member currently in it has a completed scan.
Re-scanning a member that is already done updates that member's scan id and
changes nothing else. Removing the last pending member completes the group,
because every remaining member is done.

Exactly one ``face.scan_group.completed`` per group: the transition is a
guarded ``UPDATE ... WHERE status='OPEN'`` and the event id is derived from
the group uuid, so a retry, a duplicate submit or a second worker finds
nothing to do. The owner is told once, by email through the existing
notification layer, with every member's labelled measurements — no token,
no link to a guest page, no capture. (Per-scan WhatsApp/email notices come
from ``face_scan_done`` as each member lands.)
"""
import os
import uuid
from datetime import datetime

from . import face_profiles as fp
from . import face_scan_done as fsd
from . import face_scan_invites as fsi

ST_OPEN = "OPEN"
ST_COMPLETED = "COMPLETED"
ST_CANCELLED = "CANCELLED"

MB_PENDING = "PENDING"
MB_COMPLETED = "COMPLETED"

EV_CREATED = "face.scan_group.created"
EV_MEMBER_ADDED = "face.scan_group.member_added"
EV_MEMBER_REMOVED = "face.scan_group.member_removed"
EV_MEMBER_COMPLETED = "face.scan_group.member_completed"
EV_COMPLETED = "face.scan_group.completed"
EV_CANCELLED = "face.scan_group.cancelled"
EV_NOTIFIED = "face.scan_group.notified"
EV_NOTIFY_FAILED = "face.scan_group.notify_failed"

MAX_MEMBERS = 12

GROUPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS face_scan_groups (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    group_uuid    CHAR(36) NOT NULL,
    customer_id   INT NOT NULL,
    status        VARCHAR(12) NOT NULL DEFAULT 'OPEN',
    site_host     VARCHAR(120) NULL,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at  DATETIME NULL,
    cancelled_at  DATETIME NULL,
    updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                  ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_fsg_uuid (group_uuid),
    KEY idx_fsg_customer (customer_id, status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

MEMBERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS face_scan_group_members (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    group_id         BIGINT UNSIGNED NOT NULL,
    face_profile_id  BIGINT UNSIGNED NOT NULL,
    status           VARCHAR(12) NOT NULL DEFAULT 'PENDING',
    scan_id          BIGINT UNSIGNED NULL,
    completed_at     DATETIME NULL,
    added_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_fsgm_member (group_id, face_profile_id),
    KEY idx_fsgm_profile (face_profile_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (("face_scan_groups", GROUPS_SCHEMA),
          ("face_scan_group_members", MEMBERS_SCHEMA))

_SCHEMA_READY = False

EMAIL_SUBJECT = "Optiwar — All {count} face scans complete"
EMAIL_TEXT = (
    "Hello {name},\n\n"
    "Every face scan in your request is complete and saved to your Optiwar "
    "profile.\n\n"
    "{members}\n"
    "You can now see frames recommended for each person under My Faces:\n"
    "{url}\n\n"
    "If you did not expect this, please contact Optiwar Support.\n\n"
    "Regards,\n"
    "Optiwar Support\n"
    "Factory Outlet Opticals\n"
)
EMAIL_MEMBER_TEXT = (
    "{person}\n"
    "  PD (distance):          {pd_far} mm\n"
    "  PD (near):              {pd_near} mm\n"
    "  Face width:             {face_width} mm\n"
    "  Recommended frame size: {size}\n"
)
EMAIL_HTML = (
    "<p>Hello {name},</p>"
    "<p>Every face scan in your request is complete and saved to your Optiwar "
    "profile.</p>"
    "{members}"
    "<p>You can now see frames recommended for each person under My Faces:<br>"
    "<a href='{url}'>{url}</a></p>"
    "<p>If you did not expect this, please contact Optiwar Support.</p>"
    "<p>Regards,<br>Optiwar Support<br>Factory Outlet Opticals</p>"
)
EMAIL_MEMBER_HTML = (
    "<p><strong>{person}</strong></p>"
    "<table cellpadding='4' style='border-collapse:collapse'>"
    "<tr><td>PD (distance)</td><td><strong>{pd_far} mm</strong></td></tr>"
    "<tr><td>PD (near)</td><td><strong>{pd_near} mm</strong></td></tr>"
    "<tr><td>Face width</td><td><strong>{face_width} mm</strong></td></tr>"
    "<tr><td>Recommended frame size</td><td><strong>{size}</strong></td></tr>"
    "</table>"
)


class GroupError(fp.ProfileError):
    pass


class GroupNotFound(GroupError):
    def __init__(self):
        super().__init__("not_found", "Scan group not found", 404)


def ensure_schema(db):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    fsi.ensure_schema(db)
    cur = db.cursor()
    for _name, ddl in TABLES:
        cur.execute(ddl)
    db.commit()
    _SCHEMA_READY = True


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def _group_row(cur, customer_id, group_uuid, lock=False):
    cur.execute("SELECT * FROM face_scan_groups WHERE group_uuid=%s AND customer_id=%s"
                + (" FOR UPDATE" if lock else ""), (str(group_uuid)[:36], int(customer_id)))
    return cur.fetchone()


def require_group(db, customer_id, group_uuid, lock=False):
    row = _group_row(db.cursor(), customer_id, group_uuid, lock=lock)
    if not row:
        raise GroupNotFound()
    return row


def members(db, group_row):
    cur = db.cursor()
    cur.execute(
        "SELECT m.*, p.display_name, p.relationship_type, p.is_self "
        "FROM face_scan_group_members m JOIN face_profiles p ON p.id=m.face_profile_id "
        "WHERE m.group_id=%s ORDER BY m.added_at, m.id", (int(group_row["id"]),))
    return cur.fetchall()


def view(db, group_row):
    """The API shape: state, members (with each member's person label and
    pending scan request, if any), and the distinct-member tally."""
    mem = members(db, group_row)
    invites = fsi.for_customer(db, group_row["customer_id"])
    out_members = []
    for m in mem:
        inv = invites.get(int(m["face_profile_id"]))
        if inv and inv.get("scan_group_id") != group_row["group_uuid"]:
            inv = None
        out_members.append({
            "face_profile_id": int(m["face_profile_id"]),
            "person": fsd.person_label(m),
            "status": m["status"],
            "scan_id": int(m["scan_id"]) if m["scan_id"] else None,
            "completed_at": m["completed_at"].isoformat() if m["completed_at"] else None,
            "scan_request": fsi.public_view(inv),
        })
    done = sum(1 for m in mem if m["status"] == MB_COMPLETED)
    return {
        "group_uuid": group_row["group_uuid"],
        "status": group_row["status"],
        "members": out_members,
        "required": len(mem),
        "completed": done,
        "created_at": group_row["created_at"].isoformat() if group_row["created_at"] else None,
        "completed_at": (group_row["completed_at"].isoformat()
                         if group_row["completed_at"] else None),
    }


def get(db, customer_id, group_uuid):
    return view(db, require_group(db, customer_id, group_uuid))


def for_customer(db, customer_id, status=ST_OPEN):
    cur = db.cursor()
    cur.execute("SELECT * FROM face_scan_groups WHERE customer_id=%s AND status=%s "
                "ORDER BY created_at DESC", (int(customer_id), status))
    return [view(db, r) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

def _emit(db, event_type, group_row, profile_id=None, payload=None, scan_id=None,
          suffix="", commit=True):
    return fsi.emit(
        db, event_type,
        {"customer_id": group_row["customer_id"], "face_profile_id": profile_id,
         "request_uuid": None, "scan_group_id": group_row["group_uuid"]},
        payload, event_id=fsi.event_id_for(event_type, group_row["group_uuid"], suffix),
        scan_id=scan_id, commit=commit)


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

def _clean_profile_ids(db, customer_id, profile_ids):
    ids = []
    for pid in profile_ids or []:
        row = fp.require_profile(db, customer_id, pid)
        if int(row["id"]) not in ids:
            ids.append(int(row["id"]))
    if not ids:
        raise GroupError("no_members", "Choose at least one person to scan", 400)
    if len(ids) > MAX_MEMBERS:
        raise GroupError("too_many", "A scan group can hold at most %d people" % MAX_MEMBERS, 400)
    return ids


def create(db, customer_id, profile_ids, site_host=None):
    """A new open group holding the given (distinct, owned) profiles."""
    ids = _clean_profile_ids(db, customer_id, profile_ids)
    guuid = str(uuid.uuid4())
    cur = db.cursor()
    try:
        cur.execute("INSERT INTO face_scan_groups (group_uuid, customer_id, status, site_host) "
                    "VALUES (%s,%s,%s,%s)", (guuid, int(customer_id), ST_OPEN,
                                             (site_host or "")[:120] or None))
        gid = cur.lastrowid
        for pid in ids:
            cur.execute("INSERT INTO face_scan_group_members (group_id, face_profile_id, status) "
                        "VALUES (%s,%s,%s)", (gid, pid, MB_PENDING))
        row = {"id": gid, "group_uuid": guuid, "customer_id": int(customer_id)}
        _emit(db, EV_CREATED, row, payload={"members": ids}, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return require_group(db, customer_id, guuid)


def _require_open(db, customer_id, group_uuid):
    row = require_group(db, customer_id, group_uuid, lock=True)
    if row["status"] != ST_OPEN:
        db.rollback()
        raise GroupError("closed", "This scan group is %s" % row["status"].lower(), 409)
    return row


def add_member(db, customer_id, group_uuid, profile_id):
    row = _require_open(db, customer_id, group_uuid)
    prof = fp.require_profile(db, customer_id, profile_id)
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM face_scan_group_members WHERE group_id=%s",
                (int(row["id"]),))
    if int(cur.fetchone()["n"]) >= MAX_MEMBERS:
        db.rollback()
        raise GroupError("too_many", "A scan group can hold at most %d people" % MAX_MEMBERS, 400)
    try:
        cur.execute("INSERT IGNORE INTO face_scan_group_members "
                    "(group_id, face_profile_id, status) "
                    "VALUES (%s,%s,%s)", (int(row["id"]), int(prof["id"]), MB_PENDING))
        if cur.rowcount == 1:
            _emit(db, EV_MEMBER_ADDED, row, profile_id=int(prof["id"]),
                  suffix="add:%d:%s" % (int(prof["id"]), datetime.now().isoformat()),
                  commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return view(db, require_group(db, customer_id, group_uuid))


def remove_member(db, customer_id, group_uuid, profile_id, notifier=None):
    """Drop a member. Its group-bound scan request is cancelled. If everyone
    left is already done, the group completes — every required member is."""
    row = _require_open(db, customer_id, group_uuid)
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM face_scan_group_members WHERE group_id=%s AND face_profile_id=%s",
                    (int(row["id"]), int(profile_id)))
        removed = cur.rowcount == 1
        if removed:
            _emit(db, EV_MEMBER_REMOVED, row, profile_id=int(profile_id),
                  suffix="remove:%d:%s" % (int(profile_id), datetime.now().isoformat()),
                  commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    if removed:
        _cancel_group_invite(db, customer_id, int(profile_id), row["group_uuid"])
        _try_complete(db, customer_id, row["group_uuid"], notifier=notifier)
    return view(db, require_group(db, customer_id, group_uuid))


def _cancel_group_invite(db, customer_id, profile_id, group_uuid):
    cur = db.cursor()
    cur.execute("SELECT request_uuid FROM face_scan_invites WHERE customer_id=%s "
                "AND face_profile_id=%s AND scan_group_id=%s AND status IN (%s,%s,%s)",
                (int(customer_id), int(profile_id), group_uuid) + fsi.USABLE)
    for inv in cur.fetchall():
        fsi.cancel(db, customer_id, inv["request_uuid"], reason="group_member_removed")


def cancel(db, customer_id, group_uuid, reason="sender"):
    row = _require_open(db, customer_id, group_uuid)
    cur = db.cursor()
    try:
        cur.execute("UPDATE face_scan_groups SET status=%s, cancelled_at=NOW() "
                    "WHERE id=%s AND status=%s", (ST_CANCELLED, int(row["id"]), ST_OPEN))
        _emit(db, EV_CANCELLED, row, payload={"reason": reason}, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    for m in members(db, row):
        if m["status"] == MB_PENDING:
            _cancel_group_invite(db, customer_id, int(m["face_profile_id"]), row["group_uuid"])
    return view(db, require_group(db, customer_id, group_uuid))


def drop_profile(db, customer_id, profile_id, notifier=None):
    """A deleted profile leaves every open group it was in (and may thereby
    complete one). Called from profile deletion, after the invite cancel."""
    cur = db.cursor()
    cur.execute("SELECT g.group_uuid FROM face_scan_group_members m "
                "JOIN face_scan_groups g ON g.id=m.group_id "
                "WHERE m.face_profile_id=%s AND g.customer_id=%s AND g.status=%s",
                (int(profile_id), int(customer_id), ST_OPEN))
    uuids = [r["group_uuid"] for r in cur.fetchall()]
    for guuid in uuids:
        remove_member(db, customer_id, guuid, profile_id, notifier=notifier)
    return len(uuids)


# --------------------------------------------------------------------------
# scans landing in a group
# --------------------------------------------------------------------------

def record_member_scan(db, customer_id, group_uuid, profile_id, measurements,
                       capture_path=None, algorithm_version=None, on_landed=None):
    """The owner scans a member here and now (the try-on page, this device).
    The scan row carries the group id; ``on_landed`` (default
    ``on_scan_completed``) then advances the group after the scan's commit."""
    row = require_group(db, customer_id, group_uuid)
    if row["status"] != ST_OPEN:
        raise GroupError("closed", "This scan group is %s" % row["status"].lower(), 409)
    cur = db.cursor()
    cur.execute("SELECT 1 FROM face_scan_group_members WHERE group_id=%s AND face_profile_id=%s",
                (int(row["id"]), int(profile_id)))
    if not cur.fetchone():
        raise GroupError("not_member", "That person is not in this scan group", 404)
    sid = fp.record_scan(db, customer_id, profile_id, measurements, source=fp.SRC_TRYON,
                         capture_path=capture_path, algorithm_version=algorithm_version,
                         scan_group_id=row["group_uuid"])
    (on_landed or on_scan_completed)(db, customer_id, int(profile_id), sid, row["group_uuid"])
    return sid


def on_scan_completed(db, customer_id, profile_id, scan_id, scan_group_id, notifier=None):
    """A scan with ``scan_group_id`` landed (already committed). Mark that one
    member done — a re-scan just moves its scan id — then see whether every
    distinct member is done. Returns the group view, or None when the scan
    was not a group scan or the group is not this customer's open group."""
    if not scan_group_id:
        return None
    row = _group_row(db.cursor(), customer_id, scan_group_id)
    if not row or row["status"] != ST_OPEN:
        return None
    cur = db.cursor()
    try:
        cur.execute("UPDATE face_scan_group_members SET status=%s, scan_id=%s, completed_at=NOW() "
                    "WHERE group_id=%s AND face_profile_id=%s",
                    (MB_COMPLETED, int(scan_id), int(row["id"]), int(profile_id)))
        if cur.rowcount:
            _emit(db, EV_MEMBER_COMPLETED, row, profile_id=int(profile_id),
                  payload={"scan_id": int(scan_id)}, scan_id=int(scan_id),
                  suffix="member:%d:scan:%d" % (int(profile_id), int(scan_id)), commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _try_complete(db, customer_id, row["group_uuid"], notifier=notifier)


def _try_complete(db, customer_id, group_uuid, notifier=None):
    """Complete the group iff no member is pending — under the row lock, with
    a guarded UPDATE, so two racing scans produce one transition."""
    cur = db.cursor()
    row = _group_row(cur, customer_id, group_uuid, lock=True)
    if not row or row["status"] != ST_OPEN:
        db.rollback()
        return view(db, row) if row else None
    cur.execute("SELECT COUNT(*) AS n, SUM(status=%s) AS done "
                "FROM face_scan_group_members WHERE group_id=%s", (MB_COMPLETED, int(row["id"])))
    tally = cur.fetchone()
    total, done = int(tally["n"] or 0), int(tally["done"] or 0)
    if total == 0 or done < total:
        db.rollback()
        return view(db, row)
    try:
        cur.execute("UPDATE face_scan_groups SET status=%s, completed_at=NOW() "
                    "WHERE id=%s AND status=%s", (ST_COMPLETED, int(row["id"]), ST_OPEN))
        transitioned = cur.rowcount == 1
        if transitioned:
            _emit(db, EV_COMPLETED, row, payload={"members": total}, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    row = require_group(db, customer_id, group_uuid)
    if transitioned:
        (notifier or notify)(db, row)
    return view(db, row)


# --------------------------------------------------------------------------
# the owner is told once
# --------------------------------------------------------------------------

def notify(db, group_row, environ=None, mailer=None):
    """One email to the account with every member's labelled result. Runs
    after the completion commit; a provider failure is a recorded event."""
    env = os.environ if environ is None else environ
    acct = fsd.account_for(db, group_row["customer_id"])
    if not acct or not fsd.enabled_for(acct.get("customer_email"), env):
        return {"sent": False, "reason": "gated"}
    email = fsd._clean(fsi.clean_email, acct.get("customer_email"))
    if not email:
        return {"sent": False, "reason": "no_email"}
    mem = members(db, group_row)
    blocks_t, blocks_h = [], []
    for m in mem:
        meas = fsd.measurements_for(db, m["scan_id"]) if m["scan_id"] else None
        if not meas:
            continue
        fields = dict(meas, person=fsd.person_label(m))
        blocks_t.append(EMAIL_MEMBER_TEXT.format(**fields))
        blocks_h.append(EMAIL_MEMBER_HTML.format(
            **{k: fsi._html(str(v)) for k, v in fields.items()}))
    name = (acct.get("customer_name") or "").strip() or "Customer"
    url = fsd.my_faces_url(group_row.get("site_host"))
    mail = mailer or fsi._default_mailer
    try:
        mail(email, EMAIL_SUBJECT.format(count=len(mem)),
             EMAIL_HTML.format(name=fsi._html(name), members="".join(blocks_h), url=url),
             EMAIL_TEXT.format(name=name, members="\n".join(blocks_t), url=url),
             sender=env.get(fsi.MAIL_SENDER_ENV, fsi.DEFAULT_MAIL_SENDER))
        _emit(db, EV_NOTIFIED, group_row, payload={"channel": "email", "members": len(mem)},
              suffix="email")
        return {"sent": True, "email": True}
    except Exception as exc:  # noqa: BLE001 - provider failure is a record, not a crash
        _emit(db, EV_NOTIFY_FAILED, group_row,
              payload={"channel": "email", "error": str(exc)[:160]}, suffix="email")
        return {"sent": True, "email": False}
