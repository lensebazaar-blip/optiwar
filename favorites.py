"""A customer's saved products, and — for a frame — who each one was saved for.

Favourites used to live only in the browser (``localStorage``). They are now
rows the customer owns: one per (customer, product), so the same heart is
seen from every device, and a frame can carry the person it was saved for
(``face_profile_id``: an own profile id, NULL for nobody). The person is a
label on the favourite, not part of its identity — a favourite is re-pointed
at someone else or at nobody without being removed, and deleting the
profile leaves the favourite in place with no person on it.

The browser's list is folded in idempotently (``sync``): ids it holds that
the server lacks are added, nothing is ever removed on its say-so, so an old
browser with a stale list cannot delete what another device saved.
"""
from datetime import datetime

from . import face_cart
from . import face_fit
from . import face_profiles as fp

NOBODY = face_cart.NOBODY
NO_PERSON_LABEL = face_cart.NO_PERSON_LABEL

SCHEMA = """
CREATE TABLE IF NOT EXISTS customer_favorites (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    product_id INT NOT NULL,
    face_profile_id BIGINT UNSIGNED NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE KEY uq_customer_favorite (customer_id, product_id),
    KEY ix_favorite_profile (customer_id, face_profile_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (("customer_favorites", SCHEMA),)

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


class FavoriteError(fp.ProfileError):
    pass


def _product_id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise FavoriteError("bad_product", "Which product?", 400)
    try:
        pid = int(value)
    except ValueError:
        raise FavoriteError("bad_product", "Which product?", 400)
    if pid <= 0:
        raise FavoriteError("bad_product", "Which product?", 400)
    return pid


def _product_ids(values):
    if not isinstance(values, (list, tuple)):
        raise FavoriteError("bad_product", "Which products?", 400)
    out = []
    for v in values:
        try:
            pid = _product_id(v)
        except FavoriteError:
            continue                    # a stray value in a browser list is dropped
        if pid not in out:
            out.append(pid)
    return out


def _now():
    return datetime.utcnow().replace(microsecond=0)


def catalogue(cursor, product_ids):
    """The products among these ids that exist, by id, with what the fit
    engine needs of them."""
    if not product_ids:
        return {}
    cursor.execute("SELECT product_id, product_size, product_category FROM products "
                   "WHERE product_id IN (%s)" % ",".join(["%s"] * len(product_ids)),
                   tuple(product_ids))
    return {int(r["product_id"]): r for r in cursor.fetchall()}


def _frame_ids(cursor, product_ids):
    """Which of these products a face can be fitted to."""
    return {pid for pid, row in catalogue(cursor, product_ids).items()
            if face_cart.is_frame_line(row)}


def _known(cursor, product_id):
    if product_id not in catalogue(cursor, [product_id]):
        raise FavoriteError("unknown_product", "That product does not exist", 404)


def _person(db, customer_id, cursor, product_id, profile_id):
    """The person a favourite may carry: an own profile on a frame, NULL
    otherwise; a stranger's profile is a 404 like a missing one."""
    pid = face_cart._normalise(profile_id)
    if pid == NOBODY:
        return None
    fp.get_profile(db, customer_id, pid)
    if product_id not in _frame_ids(cursor, [product_id]):
        raise FavoriteError("not_a_frame", "Only a spectacle frame is saved for a person", 400)
    return pid


def list_for(db, customer_id):
    cur = db.cursor()
    try:
        cur.execute("SELECT product_id, face_profile_id FROM customer_favorites "
                    "WHERE customer_id=%s ORDER BY id", (int(customer_id),))
        return [{"product_id": int(r["product_id"]),
                 "face_profile_id": int(r["face_profile_id"]) if r["face_profile_id"] else NOBODY}
                for r in cur.fetchall()]
    finally:
        cur.close()


def add(db, customer_id, product_id, profile_id=None):
    """Save a product; saving one already saved keeps the row and, when a
    person is given, moves it to that person. Returns the favourite."""
    product_id = _product_id(product_id)
    cur = db.cursor()
    try:
        _known(cur, product_id)
        person = _person(db, customer_id, cur, product_id, profile_id)
        now = _now()
        cur.execute("INSERT INTO customer_favorites (customer_id, product_id, "
                    "face_profile_id, created_at, updated_at) VALUES (%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE face_profile_id=COALESCE(VALUES(face_profile_id), "
                    "face_profile_id), updated_at=VALUES(updated_at)",
                    (int(customer_id), product_id, person, now, now))
        db.commit()
    finally:
        cur.close()
    return {"product_id": product_id, "face_profile_id": person or NOBODY}


def remove(db, customer_id, product_id):
    product_id = _product_id(product_id)
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM customer_favorites WHERE customer_id=%s AND product_id=%s",
                    (int(customer_id), product_id))
        gone = cur.rowcount
        db.commit()
    finally:
        cur.close()
    return bool(gone)


def sync(db, customer_id, product_ids):
    """Fold a browser's list into the customer's: adds what is missing,
    never removes. Idempotent — the same list twice changes nothing more.
    Returns the customer's full list afterwards."""
    wanted = _product_ids(product_ids)
    have = {f["product_id"] for f in list_for(db, customer_id)}
    missing = [p for p in wanted if p not in have]
    if missing:
        cur = db.cursor()
        try:
            missing = [p for p in missing if p in catalogue(cur, missing)]
            now = _now()
            cur.executemany("INSERT IGNORE INTO customer_favorites (customer_id, product_id, "
                            "face_profile_id, created_at, updated_at) VALUES (%s,%s,NULL,%s,%s)",
                            [(int(customer_id), p, now, now) for p in missing])
            db.commit()
        finally:
            cur.close()
    return list_for(db, customer_id)


def assign(db, customer_id, product_id, profile_id):
    """Point a saved frame at one of the customer's people, or at nobody.
    The favourite itself is untouched; a product not saved is a 404."""
    product_id = _product_id(product_id)
    cur = db.cursor()
    try:
        cur.execute("SELECT id FROM customer_favorites WHERE customer_id=%s AND product_id=%s",
                    (int(customer_id), product_id))
        if not cur.fetchone():
            raise FavoriteError("not_saved", "That product is not in your favourites", 404)
        person = _person(db, customer_id, cur, product_id, profile_id)
        cur.execute("UPDATE customer_favorites SET face_profile_id=%s, updated_at=%s "
                    "WHERE customer_id=%s AND product_id=%s",
                    (person, _now(), int(customer_id), product_id))
        db.commit()
    finally:
        cur.close()
    return {"product_id": product_id, "face_profile_id": person or NOBODY}


def count_references(db, customer_id, profile_id):
    cur = db.cursor()
    try:
        cur.execute("SELECT COUNT(*) AS n FROM customer_favorites "
                    "WHERE customer_id=%s AND face_profile_id=%s",
                    (int(customer_id), int(profile_id)))
        return int(cur.fetchone()["n"])
    finally:
        cur.close()


def release_profile(db, customer_id, profile_id):
    """Every favourite saved for this person stays saved, for nobody.
    Returns how many were re-pointed."""
    cur = db.cursor()
    try:
        cur.execute("UPDATE customer_favorites SET face_profile_id=NULL, updated_at=%s "
                    "WHERE customer_id=%s AND face_profile_id=%s",
                    (_now(), int(customer_id), int(profile_id)))
        n = cur.rowcount
        db.commit()
    finally:
        cur.close()
    return n


def decorate(db, customer_id, products, gated):
    """What the Favourites page draws on each saved frame for a multi-person
    account: the person it was saved for and the fit for that person, keyed
    by product id, plus the people to choose from. Empty when not gated."""
    if not gated:
        return None
    saved = {f["product_id"]: f["face_profile_id"] for f in list_for(db, customer_id)}
    lines = {}
    for product in products:
        pid = int(product["product_id"])
        if not face_cart.is_frame_line(product):
            continue
        person = saved.get(pid, NOBODY)
        try:
            fit = face_fit.evaluate_frame_fit(db, customer_id, person, product)
        except fp.ProfileError:
            fit = face_fit.evaluate_frame_fit(db, customer_id, NOBODY, product)
        lines[pid] = {"profile_id": (fit["profile"] or {}).get("id", NOBODY),
                      "profile": fit["profile"], "fit": fit}
    people = [face_fit._profile_ref(r) for r in fp.list_profiles(db, customer_id)]
    return {"lines": lines, "people": people, "nobody_label": NO_PERSON_LABEL}
