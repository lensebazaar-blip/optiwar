"""Which person a cart line is for, and what an order keeps of that.

A frame in the cart is for one of the customer's people, or for nobody
("No person / Gift"); the choice lives on the cart line itself
(``face_profile_id``: an own profile id, 0 for nobody, absent until the line
is first seen by a multi-person account). Changing it changes the fit shown
against that line and nothing else — never the product, price, quantity,
prescription or lens options, which are commercial facts the choice does
not touch.

An order keeps the meaning at the time it was placed: ``order_face_snapshots``
holds the person's name, relationship, measurements and the fit verdict per
order line, write-once. Renaming, rescanning or deleting the profile later
changes nothing there; the live ``face_profile_id`` stays only as a way to
navigate back to a profile that may no longer exist.

Deleting a profile resets the live cart lines that pointed at it to nobody
(session cart by the caller, the persisted cart here); orders are not touched.
"""
import json
from datetime import datetime

from . import face_fit
from . import face_profiles as fp
from . import lens_cart

LINE_KEY = "face_profile_id"
NOBODY = 0
NO_PERSON_LABEL = "No person / Gift"

SNAPSHOTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_face_snapshots (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    order_id VARCHAR(64) NOT NULL,
    line_no INT NOT NULL,
    product_id INT NOT NULL,
    customer_id INT NOT NULL,
    face_profile_id BIGINT UNSIGNED NULL,
    display_name VARCHAR(80) NULL,
    relationship_type VARCHAR(32) NULL,
    is_self TINYINT(1) NOT NULL DEFAULT 0,
    face_scan_id BIGINT UNSIGNED NULL,
    measured_at DATETIME NULL,
    measurements TEXT NULL,
    product_size VARCHAR(40) NULL,
    fit_classification VARCHAR(20) NOT NULL,
    fit_label VARCHAR(40) NOT NULL,
    fit_json TEXT NULL,
    snapshot_at DATETIME NOT NULL,
    UNIQUE KEY uq_order_face_line (order_id, line_no),
    KEY ix_order_face_customer (customer_id),
    KEY ix_order_face_profile (face_profile_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (("order_face_snapshots", SNAPSHOTS_SCHEMA),)

_SCHEMA_READY = False


def ensure_schema(db):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    cur = db.cursor()
    for _name, ddl in TABLES:
        cur.execute(ddl)
    db.commit()
    _SCHEMA_READY = True


class LineError(fp.ProfileError):
    pass


# --------------------------------------------------------------------------
# cart lines
# --------------------------------------------------------------------------

def is_frame_line(item):
    """A line a face can be fitted to: anything that is not a contact lens."""
    return not lens_cart.is_lens(item)


def _normalise(value):
    if value in (None, "", 0, "0", NOBODY):
        return NOBODY
    return int(value)


def default_lines(db, customer_id, session, cart):
    """Give every frame line a person: the one being shopped for when the
    line is first seen, nobody when that is the choice; a line whose person
    no longer exists falls back to nobody. Returns True when a line changed."""
    own = {int(r["id"]) for r in fp.list_profiles(db, customer_id)}
    active = None
    changed = False
    for item in cart:
        if not is_frame_line(item):
            continue
        if LINE_KEY not in item:
            if active is None:
                row = face_fit.active_profile(db, customer_id, session)
                active = int(row["id"]) if row else NOBODY
            item[LINE_KEY] = active
            changed = True
        elif _normalise(item[LINE_KEY]) not in own | {NOBODY}:
            item[LINE_KEY] = NOBODY
            changed = True
    return changed


def assign(db, customer_id, cart, index, product_id, profile_id):
    """Point cart line ``index`` (which must be ``product_id``, so a cart that
    changed underneath the page cannot be edited blind) at one of the
    customer's people or at nobody. Only the person changes."""
    try:
        index = int(index)
    except (TypeError, ValueError):
        raise LineError("bad_line", "Which cart line?", 400)
    try:
        item = cart[index]
    except IndexError:
        raise LineError("no_such_line", "That cart line no longer exists", 404)
    if str(item.get("product_id")) != str(product_id):
        raise LineError("line_mismatch", "The cart changed; reload the page", 409)
    if not is_frame_line(item):
        raise LineError("not_a_frame", "A contact lens is not fitted to a face", 400)
    pid = _normalise(profile_id)
    if pid != NOBODY:
        fp.get_profile(db, customer_id, pid)      # NotFound for a stranger's
    item[LINE_KEY] = pid
    return item


def line_fit(db, customer_id, item, product_size=None):
    """The fit of one cart line for its person, with the person alongside;
    a line without a choice yet is 'no person'."""
    pid = _normalise(item.get(LINE_KEY))
    if product_size is None:
        cur = db.cursor()
        try:
            product_size = _sizes(cur, [item]).get(str(item.get("product_id")))
        finally:
            cur.close()
    product = {"product_size": product_size}
    try:
        return face_fit.evaluate_frame_fit(db, customer_id, pid, product)
    except fp.ProfileError:
        return face_fit.evaluate_frame_fit(db, customer_id, NOBODY, product)


def _sizes(cursor, cart):
    ids = sorted({str(i.get("product_id")) for i in cart
                  if is_frame_line(i) and i.get("product_id") is not None})
    if not ids:
        return {}
    cursor.execute("SELECT product_id, product_size FROM products "
                   "WHERE product_id IN (%s)" % ",".join(["%s"] * len(ids)),
                   tuple(ids))
    return {str(r["product_id"]): r["product_size"] for r in cursor.fetchall()}


def decorate(db, customer_id, cart):
    """What the checkout page draws beside each frame line: the person and
    the fit, keyed by line index, plus the people to choose from."""
    cur = db.cursor()
    try:
        sizes = _sizes(cur, cart)
    finally:
        cur.close()
    lines = {}
    for index, item in enumerate(cart):
        if not is_frame_line(item):
            continue
        fit = line_fit(db, customer_id, item,
                       sizes.get(str(item.get("product_id"))))
        # the person as resolved: a deleted or foreign id reads as nobody
        lines[index] = {"profile_id": (fit["profile"] or {}).get("id", NOBODY),
                        "profile": fit["profile"], "fit": fit}
    people = [face_fit._profile_ref(r) for r in fp.list_profiles(db, customer_id)]
    return {"lines": lines, "people": people, "nobody_label": NO_PERSON_LABEL}


# --------------------------------------------------------------------------
# order snapshots
# --------------------------------------------------------------------------

def _measurements(row):
    out = {}
    for k in fp.MEASUREMENT_FIELDS:
        v = row.get(k)
        if isinstance(v, datetime):
            v = v.isoformat()
        out[k] = v
    return out


def record(db, cursor, order_id, customer_id, cart, now=None):
    """Seal each frame line's person and fit against the order, write-once
    per (order, line). Lines with no choice recorded are nobody; a lens line
    is skipped. Nothing here is read back from the live profile later."""
    if not any(LINE_KEY in i for i in cart if is_frame_line(i)):
        return 0
    profiles = {int(r["id"]): r for r in fp.list_profiles(db, customer_id)}
    sizes = _sizes(cursor, cart)
    when = now or datetime.utcnow().replace(microsecond=0)
    written = 0
    for line_no, item in enumerate(cart, start=1):
        if not is_frame_line(item):
            continue
        pid = _normalise(item.get(LINE_KEY))
        row = profiles.get(pid)
        size = sizes.get(str(item.get("product_id")))
        if row:
            fit = face_fit.evaluate(face_fit.profile_measurement(row), size)
        else:
            fit = face_fit.evaluate(None, size)
            pid = None
        cursor.execute(
            "INSERT IGNORE INTO order_face_snapshots "
            "(order_id, line_no, product_id, customer_id, face_profile_id, "
            " display_name, relationship_type, is_self, face_scan_id, "
            " measured_at, measurements, product_size, fit_classification, "
            " fit_label, fit_json, snapshot_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (str(order_id), line_no, int(item.get("product_id")), int(customer_id),
             pid,
             row["display_name"] if row else None,
             row["relationship_type"] if row else None,
             1 if row and row.get("is_self") else 0,
             row.get("latest_scan_id") if row else None,
             row.get("measured_at") if row else None,
             json.dumps(_measurements(row), default=str) if row and row.get("pd_far") is not None else None,
             size, fit["classification"], fit["label"],
             json.dumps({k: fit[k] for k in ("classification", "label", "matched",
                                             "recommended_dimensions",
                                             "actual_dimensions",
                                             "measurement_delta", "reasons")},
                        default=str),
             when))
        written += cursor.rowcount if cursor.rowcount > 0 else 0
    return written


def for_order(cursor, order_id):
    """The sealed lines of an order, in line order; [] for an order placed
    before people were assigned to lines."""
    cursor.execute(
        "SELECT line_no, product_id, face_profile_id, display_name, "
        "relationship_type, is_self, face_scan_id, measured_at, measurements, "
        "product_size, fit_classification, fit_label, fit_json, snapshot_at "
        "FROM order_face_snapshots WHERE order_id=%s ORDER BY line_no",
        (str(order_id),))
    rows = []
    for r in cursor.fetchall():
        r = dict(r)
        for k in ("measurements", "fit_json"):
            try:
                r[k] = json.loads(r[k]) if r.get(k) else None
            except ValueError:
                r[k] = None
        r["fit"] = r.pop("fit_json")
        r["person"] = (r["display_name"] if r["display_name"]
                       else NO_PERSON_LABEL)
        rows.append(r)
    return rows


# --------------------------------------------------------------------------
# live references
# --------------------------------------------------------------------------

def _persisted(cursor, customer_id):
    cursor.execute("SELECT cart_json FROM persistent_cart WHERE customer_id=%s",
                   (int(customer_id),))
    row = cursor.fetchone()
    if not row or not row.get("cart_json"):
        return None
    try:
        cart = json.loads(row["cart_json"])
    except ValueError:
        return None
    return cart if isinstance(cart, list) else None


def _pointing_at(cart, profile_id):
    return [i for i in (cart or []) if is_frame_line(i)
            and _normalise(i.get(LINE_KEY)) == int(profile_id)
            and int(profile_id) != NOBODY]


def count_references(db, customer_id, profile_id, session_cart=None):
    """Live cart lines for this person: the persisted cart, or the session's
    when the persisted one is absent (the session is what checkout reads)."""
    cur = db.cursor()
    try:
        cart = _persisted(cur, customer_id)
    finally:
        cur.close()
    if cart is None:
        cart = session_cart
    return len(_pointing_at(cart, profile_id))


def release_profile(db, customer_id, profile_id, session_cart=None):
    """Reset every live cart line for this person to nobody — the persisted
    cart in the database and, if given, the session's — so the line stays
    in the cart with its product and price and only loses its face. Orders
    are never touched. Returns how many lines were reset."""
    reset = 0
    for item in _pointing_at(session_cart, profile_id):
        item[LINE_KEY] = NOBODY
        reset += 1
    cur = db.cursor()
    try:
        cart = _persisted(cur, customer_id)
        hits = _pointing_at(cart, profile_id)
        if hits:
            for item in hits:
                item[LINE_KEY] = NOBODY
            cur.execute("UPDATE persistent_cart SET cart_json=%s WHERE customer_id=%s",
                        (json.dumps(cart, default=str), int(customer_id)))
            db.commit()
            reset = max(reset, len(hits))
    finally:
        cur.close()
    return reset
