"""A customer's return request, and Ops' decision on it (reverse pickup, phase 3a).

The signed-in owner of a delivered India order asks for a return in My Orders
within RETURN_WINDOW_DAYS of delivery: a reason, a description, photos (at
least two for a manufacturing defect) and the versioned declarations. That
opens a return case with request_status SUBMITTED and fee DUE. Ops then
approves it subject to inspection, asks for more information, or declines it;
a pickup is booked only once the request is approved and the fee is PAID or
WAIVED. Photos are kept outside the web root and reached by Ops only through a
signed, expiring link that is audited on use.
"""
import datetime
import hashlib
import hmac
import os
import time
import uuid

try:
    from . import chat_attachments, chat_image, policy_terms, reship
    from . import reverse_pickup as rp
    from .paid_orders import add_history
except ImportError:  # pragma: no cover - flat layout
    import chat_attachments
    import chat_image
    import policy_terms
    import reship
    import reverse_pickup as rp
    from paid_orders import add_history

RETURN_WINDOW_DAYS = 7
DELIVERED_STATUS = "Complete"

REASONS = {
    "MANUFACTURING_DEFECT": "Manufacturing defect",
    "WRONG_ITEM": "Wrong item received",
    "DAMAGED_ON_ARRIVAL": "Damaged on arrival",
    "OTHER": "Other",
}
PHOTOS_REQUIRED = {"MANUFACTURING_DEFECT": 2}
MAX_PHOTOS = 6
PHOTO_KINDS = ("jpeg", "png", "webp")
MIN_DESCRIPTION = 20
MAX_DESCRIPTION = 2000
MIN_NOTE = 10

DECLARATIONS_VERSION = "rp-declarations-2026-10-03"
DECLARATIONS = (
    "The product is in its original box/case with all accessories, and the invoice is included.",
    "The product has not been altered, repaired or damaged by me after delivery.",
    "I understand a \u20b9250 reverse-pickup fee applies. It is refunded only if Optiwar's "
    "inspection confirms a manufacturing defect.",
    "I will hand the parcel only to the Delhivery agent for the pickup Optiwar books, and will "
    "not ship it myself.",
    "I understand that Optiwar's inspection decides the outcome, and that approval to return is "
    "not a promise of a refund.",
)
POLICY_KIND = "rp_declarations"
POLICY_SITE = policy_terms.SITE_IN

DECISIONS = (rp.REQ_APPROVED, rp.REQ_INFO, rp.REQ_DECLINED)
PHOTO_LINK_TTL = 10 * 60
EV_PHOTO_VIEWED = "reverse_pickup.request_photo_viewed"

CARD_CAN_REQUEST = "CAN_REQUEST"
CARD_SUBMITTED = "SUBMITTED"
CARD_INFO = "INFO_REQUESTED"
CARD_APPROVED_FEE_DUE = "APPROVED_FEE_DUE"
CARD_APPROVED = "APPROVED"
CARD_NOT_APPROVED = "NOT_APPROVED"
CARD_FEE_REFUNDED = "FEE_REFUNDED"
CARD_SHIPPED = "SHIPPED_TO_CUSTOMER"
CARD_COMPLETED = "COMPLETED"

PHOTOS_DDL = """CREATE TABLE IF NOT EXISTS reverse_pickup_photos (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    case_uuid     CHAR(36) NOT NULL,
    order_id      VARCHAR(64) NOT NULL,
    position      TINYINT UNSIGNED NOT NULL,
    stored_name   VARCHAR(128) NOT NULL,
    original_name VARCHAR(128) NULL,
    sha256        CHAR(64) NOT NULL,
    byte_size     INT NOT NULL,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_rpp_case_pos (case_uuid, position),
    KEY idx_rpp_order (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

TABLES = (("reverse_pickup_photos", PHOTOS_DDL),)

_SCHEMA_READY = False


def declarations_text():
    return "\n".join("%d. %s" % (i, line) for i, line in enumerate(DECLARATIONS, 1))


def declarations_sha256():
    return policy_terms.sha(declarations_text())


def ensure_schema(db):
    """The phase-3 columns and the photo table, and the declarations sealed
    into policy_versions once, so an old request's text is reproducible."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    rp.ensure_schema(db)
    cur = db.cursor()
    for _name, ddl in TABLES:
        cur.execute(ddl)
    policy_terms.ensure_schema(cur)
    cur.execute("INSERT IGNORE INTO policy_versions (kind, site, version, sha256, body) "
                "VALUES (%s,%s,%s,%s,%s)", (POLICY_KIND, POLICY_SITE, DECLARATIONS_VERSION,
                                            declarations_sha256(), declarations_text()))
    db.commit()
    _SCHEMA_READY = True


def form_context():
    """What the My Orders form shows; the server checks every value again."""
    return {"reasons": [{"code": k, "label": v} for k, v in REASONS.items()],
            "photos_required": dict(PHOTOS_REQUIRED), "max_photos": MAX_PHOTOS,
            "min_description": MIN_DESCRIPTION, "declarations": list(DECLARATIONS),
            "declarations_version": DECLARATIONS_VERSION, "window_days": RETURN_WINDOW_DAYS,
            "fee": rp.FEE_MINOR // 100}


# --------------------------------------------------------------------------
# photos
# --------------------------------------------------------------------------

def prepare_photos(files):
    """``[(filename, bytes)]`` as stored JPEGs: type from the bytes (JPEG, PNG
    or WebP), metadata stripped, size capped. Raises ReversePickupError."""
    files = [(n, d) for n, d in (files or []) if d]
    if len(files) > MAX_PHOTOS:
        raise rp.ReversePickupError("too_many_photos",
                                    "Please attach at most %d photos." % MAX_PHOTOS)
    out = []
    for name, data in files:
        try:
            meta = chat_attachments.validate(data, name)
        except chat_attachments.Rejected as exc:
            raise rp.ReversePickupError("invalid_photo", str(exc))
        if meta["kind"] not in PHOTO_KINDS:
            raise rp.ReversePickupError("invalid_photo", "Only JPEG, PNG or WebP photos can be attached.")
        try:
            jpeg = chat_image.shrink(data)
        except (chat_image.Unreadable, chat_image.TooLarge):
            raise rp.ReversePickupError("invalid_photo",
                                        "One photo could not be read. Please choose another.")
        out.append({"data": jpeg, "sha256": hashlib.sha256(jpeg).hexdigest(),
                    "byte_size": len(jpeg), "filename": chat_attachments.jpeg_name(meta["filename"])})
    return out


def stored_name(case_uuid, position):
    return "rp-%s-%d.jpg" % (case_uuid, int(position))


def photos_for(db, case_uuid):
    cur = db.cursor()
    cur.execute("SELECT * FROM reverse_pickup_photos WHERE case_uuid=%s ORDER BY position",
                (case_uuid,))
    return cur.fetchall()


def _sign(secret, case_uuid, position, expires):
    msg = ("rpphoto:%s:%d:%d" % (case_uuid, int(position), int(expires))).encode()
    return hmac.new((secret or "").encode("utf-8"), msg, hashlib.sha256).hexdigest()


def photo_link(secret, case_uuid, position, ttl=PHOTO_LINK_TTL, now=None):
    exp = int((now or time.time()) + ttl)
    return ("/ops/api/reverse-pickup/photos/%s/%d?exp=%d&sig=%s"
            % (case_uuid, int(position), exp, _sign(secret, case_uuid, position, exp)))


def link_valid(secret, case_uuid, position, exp, sig, now=None):
    if not secret:
        return False
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return False
    if exp < int(now or time.time()):
        return False
    return hmac.compare_digest(_sign(secret, case_uuid, position, exp), str(sig or ""))


def photo_views(db, case_uuid, secret, now=None):
    return [{"position": int(p["position"]), "sha256": p["sha256"], "byte_size": int(p["byte_size"]),
             "url": photo_link(secret, case_uuid, p["position"], now=now)}
            for p in photos_for(db, case_uuid)]


def record_photo_view(db, case_uuid, position, exp, actor):
    case = rp.case_by_uuid(db, case_uuid)
    if not case:
        return False
    reship.emit(db, EV_PHOTO_VIEWED, case["order_id"], case_uuid, case.get("customer_id"),
                {"position": int(position), "actor": actor}, key=case_uuid,
                suffix="%d:%s" % (int(position), exp))
    return True


# --------------------------------------------------------------------------
# eligibility
# --------------------------------------------------------------------------

def _latest_status(cur, order_ids):
    if not order_ids:
        return {}
    cur.execute("SELECT order_id, order_status_name, created_at FROM order_status WHERE order_id IN (%s) "
                "ORDER BY order_status_id" % ",".join(["%s"] * len(order_ids)), tuple(order_ids))
    return {r["order_id"]: r for r in cur.fetchall()}


def _heads(cur, order_ids):
    if not order_ids:
        return {}
    cur.execute("SELECT order_id, MIN(customer_id) AS customer_id, MIN(site_from) AS site_from, "
                "MAX(is_test) AS is_test FROM orders WHERE order_id IN (%s) GROUP BY order_id"
                % ",".join(["%s"] * len(order_ids)), tuple(order_ids))
    return {r["order_id"]: r for r in cur.fetchall()}


def _window(status, now):
    """``(code, until)``: code None when a request may be made now."""
    if not status or status["order_status_name"] != DELIVERED_STATUS:
        return "not_delivered", None
    if not status.get("created_at"):
        return "delivery_date_unknown", None
    until = status["created_at"] + datetime.timedelta(days=RETURN_WINDOW_DAYS)
    return (None if now <= until else "return_window_closed"), until


def _case_allows_request(case):
    """One open return per order; a completed (or declined) one allows another."""
    return not case or bool(case.get("completed_at"))


ELIGIBILITY_MESSAGES = {
    "not_delivered": "A return can be requested once the order has been delivered.",
    "delivery_date_unknown": "We could not confirm the delivery date of this order. Please contact support.",
    "return_window_closed": "Return requests can be made within %d days of delivery." % RETURN_WINDOW_DAYS,
    "test_order": "This order cannot be returned.",
    "return_exists": "A return is already open for this order.",
}


# --------------------------------------------------------------------------
# the customer's request
# --------------------------------------------------------------------------

def _refuse(db, code, status=409):
    db.rollback()
    raise rp.ReversePickupError(code, ELIGIBILITY_MESSAGES[code], status)


def _validate_form(form):
    code = str(form.get("reason") or "").strip().upper()
    if code not in REASONS:
        raise rp.ReversePickupError("invalid_reason", "Please choose a reason for the return.")
    description = str(form.get("description") or "").strip()
    if len(description) < MIN_DESCRIPTION:
        raise rp.ReversePickupError("description_required",
                                    "Please describe the problem in at least %d characters."
                                    % MIN_DESCRIPTION)
    if len(description) > MAX_DESCRIPTION:
        raise rp.ReversePickupError("description_too_long",
                                    "Please keep the description under %d characters." % MAX_DESCRIPTION)
    if str(form.get("declarations_version") or "") != DECLARATIONS_VERSION:
        raise rp.ReversePickupError("declarations_outdated",
                                    "The declarations have changed. Please reload the page and accept them again.")
    accepted = {str(v) for v in (form.get("declarations") or [])}
    if accepted != {str(i) for i in range(1, len(DECLARATIONS) + 1)}:
        raise rp.ReversePickupError("declarations_required", "Please accept every declaration.")
    return code, description


def submit(db, customer_id, order_id, form, photos, ip, upload_dir):
    """Open a return case from the customer's request. ``photos`` comes from
    :func:`prepare_photos`. Returns the case. Anything not the customer's own
    India order is ``not_found``."""
    code, description = _validate_form(form)
    need = PHOTOS_REQUIRED.get(code, 0)
    if len(photos) < need:
        raise rp.ReversePickupError("photos_required",
                                    "Please attach at least %d photos of the defect." % need)
    ensure_schema(db)
    cur = db.cursor()
    head = reship._order_head(cur, str(order_id or "").strip())
    if (not head or not customer_id or str(head.get("customer_id")) != str(customer_id)
            or not reship.is_india_host(head.get("site_from"))):
        raise rp.ReversePickupError("not_found", "order not found", 404)
    oid = str(order_id).strip()
    if head.get("is_test"):
        _refuse(db, "test_order")
    window, _until = _window(_latest_status(cur, [oid]).get(oid), reship.db_now(db))
    if window:
        _refuse(db, window)
    existing = rp.case_for_order(db, oid, for_update=True)
    if not _case_allows_request(existing):
        _refuse(db, "return_exists")
    case_no = int(existing["case_no"]) + 1 if existing else 1
    case_uuid = str(uuid.uuid4())
    reason = REASONS[code]
    written = []
    try:
        cur.execute(
            "INSERT INTO reverse_pickup_cases (case_uuid, order_id, case_no, customer_id, site_from, "
            "source, return_reason, fee_state, fee_amount_minor, fee_currency, request_status, "
            "reason_code, defect_description, declarations_version, declarations_sha256, "
            "declarations_accepted_at, declarations_ip, created_by) "
            "VALUES (%s,%s,%s,%s,%s,'customer',%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s,%s)",
            (case_uuid, oid, case_no, head.get("customer_id"), head.get("site_from"), reason,
             rp.FEE_DUE, rp.FEE_MINOR, rp.FEE_CURRENCY, rp.REQ_SUBMITTED, code, description,
             DECLARATIONS_VERSION, declarations_sha256(), str(ip or "")[:64] or None,
             "customer:%s" % customer_id))
        os.makedirs(upload_dir, exist_ok=True)
        for pos, photo in enumerate(photos, 1):
            name = stored_name(case_uuid, pos)
            path = os.path.join(upload_dir, name)
            with open(path, "wb") as fh:
                fh.write(photo["data"])
            written.append(path)
            cur.execute("INSERT INTO reverse_pickup_photos (case_uuid, order_id, position, stored_name, "
                        "original_name, sha256, byte_size) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (case_uuid, oid, pos, name, photo["filename"][:128], photo["sha256"],
                         photo["byte_size"]))
        case = rp.case_by_uuid(db, case_uuid)
        add_history(cur, oid, "Return requested by the customer: %s, %d photo(s), declarations %s"
                    % (reason, len(photos), DECLARATIONS_VERSION), head.get("site_from"))
        data = {"source": "customer", "reason_code": code, "reason": reason,
                "description": description, "photo_count": len(photos),
                "declarations_version": DECLARATIONS_VERSION,
                "declarations_sha256": declarations_sha256(),
                "requested_at": rp._iso(case.get("created_at"))}
        rp._audit(db, rp.EV_REQUESTED, case, data)
        rp.queue_ops_event(db, rp.EV_REQUESTED, oid, data, case=case,
                           pickup=rp.latest_for_order(db, oid), key=case_uuid, commit=False)
        rp.enqueue_notice(db, case, rp.NOTICE_REQUEST_RECEIVED)
        db.commit()
    except Exception as exc:
        db.rollback()
        for path in written:
            try:
                os.remove(path)
            except OSError:
                pass
        if rp._is_duplicate_key(exc):
            raise rp.ReversePickupError("return_exists", ELIGIBILITY_MESSAGES["return_exists"], 409)
        raise
    return case


# --------------------------------------------------------------------------
# Ops' decision
# --------------------------------------------------------------------------

def decide(db, order_id, body, operator):
    """Ops approves the request subject to inspection, asks for more
    information, or declines it. Returns ``(case, changed)``; the same
    decision again is ``changed`` False, a different final one is refused."""
    decision = str(body.get("decision") or "").strip().upper()
    if decision not in DECISIONS:
        raise rp.ReversePickupError("invalid_decision", "decision must be one of %s" % ", ".join(DECISIONS))
    note = rp._clip(body.get("note"), 1000)
    if decision != rp.REQ_APPROVED and len(note) < MIN_NOTE:
        raise rp.ReversePickupError("note_required",
                                    "%s needs a note for the customer (at least %d characters)"
                                    % (decision, MIN_NOTE))
    oid, case = rp._case_for_action(db, order_id)
    status = case.get("request_status")
    if not status:
        db.rollback()
        raise rp.ReversePickupError("no_request", "this case was not opened by a customer request", 409)
    if status == decision and (case.get("decision_note") or "") == note:
        db.rollback()
        return case, False
    if status not in rp.REQ_OPEN or (status == rp.REQ_INFO and decision == rp.REQ_INFO):
        db.rollback()
        raise rp.ReversePickupError("decision_exists", "the request is already %s" % status, 409)
    who = rp._clip(operator, 191)
    cur = db.cursor()
    if decision == rp.REQ_DECLINED:
        cur.execute("UPDATE reverse_pickup_cases SET request_status=%s, decision_note=%s, decided_by=%s, "
                    "decided_at=NOW(), completed_outcome=%s, completed_note=%s, completed_by=%s, "
                    "completed_at=NOW() WHERE id=%s",
                    (decision, note, who, rp.OUTCOME_NOT_APPROVED, note[:500], who, case["id"]))
    else:
        cur.execute("UPDATE reverse_pickup_cases SET request_status=%s, decision_note=%s, decided_by=%s, "
                    "decided_at=NOW() WHERE id=%s", (decision, note or None, who, case["id"]))
    case = rp.case_by_uuid(db, case["case_uuid"])
    add_history(cur, oid, "Return request %s by %s%s" % (decision, rp._clip(operator, 120),
                                                         ": %s" % note if note else ""),
                case.get("site_from"))
    data = {"decision": decision, "note": note or None, "decided_by": who,
            "decided_at": rp._iso(case.get("decided_at"))}
    rp._audit(db, rp.EV_REQUEST_DECIDED, case, data, suffix=decision)
    rp.queue_ops_event(db, rp.EV_REQUEST_DECIDED, oid, data, case=case,
                       pickup=rp.latest_for_order(db, oid), key=case["case_uuid"], suffix=decision,
                       commit=False)
    db.commit()
    return case, True


def decision_notice(case):
    status = (case or {}).get("request_status")
    if status == rp.REQ_APPROVED:
        return (rp.NOTICE_REQUEST_APPROVED_WAIVED if case["fee_state"] == rp.FEE_WAIVED
                else rp.NOTICE_REQUEST_APPROVED)
    if status == rp.REQ_INFO:
        return rp.NOTICE_REQUEST_INFO
    if status == rp.REQ_DECLINED:
        return rp.NOTICE_REQUEST_DECLINED
    return None


# --------------------------------------------------------------------------
# My Orders
# --------------------------------------------------------------------------

def _card(case, pickup, window, until):
    """The request card of one order, or None. Nothing Ops typed reaches it
    except the note Ops wrote for the customer (the same text the email
    carries)."""
    can = window is None and _case_allows_request(case)
    base = {"until": until, "window_days": RETURN_WINDOW_DAYS, "fee": rp.FEE_MINOR // 100}
    if case and case.get("completed_at") and case.get("completed_outcome") != rp.OUTCOME_NOT_APPROVED:
        return dict(base, state=CARD_COMPLETED, outcome=case["completed_outcome"],
                    shipment=rp.forward_view(case), completed_at=case["completed_at"],
                    fee_refunded=case["fee_state"] == rp.FEE_REFUNDED,
                    can_request=can, form=form_context() if can else None)
    if case and case.get("forward_shipped_at"):
        return dict(base, state=CARD_SHIPPED, shipment=rp.forward_view(case),
                    fee_refunded=case["fee_state"] == rp.FEE_REFUNDED, can_request=False, form=None)
    if case and case["fee_state"] == rp.FEE_REFUNDED:
        return dict(base, state=CARD_FEE_REFUNDED, can_request=False, form=None,
                    refund_id=case.get("fee_refund_id"),
                    refunded=int(case.get("fee_refunded_minor") or 0) // 100)
    if case and case.get("completed_outcome") == rp.OUTCOME_NOT_APPROVED:
        return dict(base, state=CARD_NOT_APPROVED, note=case.get("decision_note") or None,
                    can_request=can, form=form_context() if can else None)
    if case and not pickup:
        status = case.get("request_status")
        common = dict(base, reason=case.get("return_reason"),
                      requested_at=case.get("created_at"), can_request=False, form=None)
        if status == rp.REQ_SUBMITTED:
            return dict(common, state=CARD_SUBMITTED)
        if status == rp.REQ_INFO:
            return dict(common, state=CARD_INFO, note=case.get("decision_note") or None)
        if status == rp.REQ_APPROVED and not case.get("received_at"):
            if case["fee_state"] == rp.FEE_DUE:
                return dict(common, state=CARD_APPROVED_FEE_DUE, can_pay=True)
            return dict(common, state=CARD_APPROVED, fee_paid=case["fee_state"] == rp.FEE_PAID)
        return None
    if not case and can:
        return dict(base, state=CARD_CAN_REQUEST, can_request=True, form=form_context())
    return None


def customer_cards(db, customer_id, order_ids, now=None):
    """``{order_id: card}`` for the signed-in customer's own India orders."""
    ids = [str(o) for o in order_ids or []]
    if not ids or not customer_id:
        return {}
    ensure_schema(db)
    cur = db.cursor()
    now = now or reship.db_now(db)
    heads = _heads(cur, ids)
    statuses = _latest_status(cur, ids)
    marks = ",".join(["%s"] * len(ids))
    cur.execute("SELECT * FROM reverse_pickup_cases WHERE order_id IN (%s) ORDER BY case_no" % marks,
                tuple(ids))
    cases = {r["order_id"]: r for r in cur.fetchall()}
    cur.execute("SELECT * FROM order_reverse_pickups WHERE order_id IN (%s) ORDER BY id" % marks,
                tuple(ids))
    pickups = {r["order_id"]: r for r in cur.fetchall()}
    out = {}
    for oid in ids:
        head = heads.get(oid)
        if (not head or str(head.get("customer_id")) != str(customer_id) or head.get("is_test")
                or not reship.is_india_host(head.get("site_from"))):
            continue
        window, until = _window(statuses.get(oid), now)
        card = _card(cases.get(oid), pickups.get(oid), window, until)
        if card:
            out[oid] = card
    return out
