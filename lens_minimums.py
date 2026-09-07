"""Catalogue-wide contact-lens minimum boxes: one rule, one seed, one table.

The business rule is stated once, here, and derived rather than copied:

    min_both_per_eye = CEIL(min_single_eye / 2)

so the both-eye total (``2 * min_both_per_eye``) is always even and never below
the single-eye minimum. A SKU may only depart from the formula when its seed row
carries an ``exception_note`` saying why; an unexplained departure is refused,
which is what keeps the older inconsistent "both" column from creeping back.

The seed is ``lens_data/contact_lens_min_order.json`` (the owner's 59 rows,
committed verbatim). ``rows()`` reads and validates it without a database;
``seed()`` upserts it into ``contact_lens_min_order``; ``apply_to_products()``
copies each matched model's minimums onto the product profile the storefront
already reads (``min_boxes_single_eye`` / ``min_boxes_both_per_eye``), so
nothing downstream — PDP, cart, validator — learns a new lookup.

Nothing here is SKU-specific: Precision1's 12 / 6 is a row in the file.
"""
import datetime
import json
import math
import os
import re

SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "lens_data", "contact_lens_min_order.json")

SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_min_order (
    model_key          VARCHAR(80) NOT NULL PRIMARY KEY,
    model              VARCHAR(120) NOT NULL,
    min_single_eye     SMALLINT UNSIGNED NOT NULL,
    min_both_per_eye   SMALLINT UNSIGNED NOT NULL,
    source_both_value  SMALLINT UNSIGNED NULL,
    exception_note     VARCHAR(255) NULL,
    confirmed_by_owner TINYINT(1) NOT NULL DEFAULT 0,
    seed_source        VARCHAR(120) NULL,
    seeded_at          DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


class SeedError(ValueError):
    """The seed file states something the rule does not allow."""


def both_per_eye(min_single_eye):
    """The per-eye minimum when both eyes are ordered."""
    single = int(min_single_eye)
    if single < 1:
        raise SeedError("min_single_eye must be at least 1, got %r"
                        % (min_single_eye,))
    return int(math.ceil(single / 2.0))


def model_key(model):
    """A model name reduced to what two spellings of it share.

    Case, punctuation, the registered mark and spacing are not part of the
    identity: "ACUVUE® VITA®" and "Acuvue Vita" are one lens.
    """
    text = (model or "").replace("\u00ae", "").replace("\u2122", "").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _whole(value, field, model):
    if isinstance(value, bool) or not isinstance(value, int):
        raise SeedError("%s: %s must be a whole number, got %r"
                        % (model, field, value))
    return value


def rows(path=SEED_PATH):
    """The validated seed rows, in file order. Refuses the file on the first
    row that breaks the rule, so a half-right seed is never applied."""
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    products = document.get("products")
    if not isinstance(products, list) or not products:
        raise SeedError("seed has no products")
    source = (document.get("_meta") or {}).get("source")
    out, seen = [], set()
    for entry in products:
        model = (entry.get("model") or "").strip()
        if not model:
            raise SeedError("a row has no model")
        key = model_key(model)
        if key in seen:
            raise SeedError("%s: stated twice" % model)
        seen.add(key)
        single = _whole(entry.get("min_single_eye_boxes"),
                        "min_single_eye_boxes", model)
        stated_both = _whole(entry.get("min_both_eyes_boxes_per_eye"),
                             "min_both_eyes_boxes_per_eye", model)
        derived = both_per_eye(single)
        note = (entry.get("exception_note") or "").strip() or None
        if stated_both != derived and not note:
            raise SeedError("%s: both-eye minimum %d does not follow "
                            "CEIL(%d / 2) = %d and no exception_note says why"
                            % (model, stated_both, single, derived))
        out.append({
            "model_key": key,
            "model": model,
            "min_single_eye": single,
            "min_both_per_eye": stated_both if note else derived,
            "source_both_value": entry.get("source_table_both_value"),
            "exception_note": note,
            "confirmed_by_owner": 1 if entry.get("confirmed_by_owner") else 0,
            "seed_source": str(source)[:120] if source else None,
        })
    return out


def minimums_for(seed_rows, model):
    """``(single, both_per_eye)`` for a model name, or ``None`` if unstated."""
    key = model_key(model)
    for row in seed_rows:
        if row["model_key"] == key:
            return row["min_single_eye"], row["min_both_per_eye"]
    return None


def ensure_table(cursor):
    cursor.execute(SCHEMA)


def seed(cursor, seed_rows, now=None):
    """Upsert every row; returns how many were written."""
    now = now or datetime.datetime.utcnow().replace(microsecond=0)
    for row in seed_rows:
        cursor.execute(
            "INSERT INTO contact_lens_min_order (model_key, model, "
            "min_single_eye, min_both_per_eye, source_both_value, "
            "exception_note, confirmed_by_owner, seed_source, seeded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE model = VALUES(model), "
            "min_single_eye = VALUES(min_single_eye), "
            "min_both_per_eye = VALUES(min_both_per_eye), "
            "source_both_value = VALUES(source_both_value), "
            "exception_note = VALUES(exception_note), "
            "confirmed_by_owner = VALUES(confirmed_by_owner), "
            "seed_source = VALUES(seed_source), seeded_at = VALUES(seeded_at)",
            (row["model_key"], row["model"], row["min_single_eye"],
             row["min_both_per_eye"], row["source_both_value"],
             row["exception_note"], row["confirmed_by_owner"],
             row["seed_source"], now))
    return len(seed_rows)


def apply_to_products(cursor, apply=False):
    """Copy the seeded minimums onto every profile that names a model.

    Returns ``(matched, unmatched)``: rows ``(product_id, model, single, both)``
    that were (or would be) written, and profiles whose ``min_order_model`` is
    not in the seed. Nothing is written unless ``apply`` is true, and a profile
    naming no model is left exactly as it is.
    """
    # Matched in Python: model_key() is not expressible in SQL.
    cursor.execute("SELECT product_id, min_order_model FROM "
                   "contact_lens_products WHERE min_order_model IS NOT NULL "
                   "AND min_order_model <> ''")
    profiles = list(cursor.fetchall())
    cursor.execute("SELECT model_key, min_single_eye, min_both_per_eye "
                   "FROM contact_lens_min_order")
    table = {r["model_key"]: (r["min_single_eye"], r["min_both_per_eye"])
             for r in cursor.fetchall()}
    matched, unmatched = [], []
    for profile in profiles:
        found = table.get(model_key(profile["min_order_model"]))
        if not found:
            unmatched.append((profile["product_id"],
                              profile["min_order_model"]))
            continue
        single, both = found
        matched.append((profile["product_id"], profile["min_order_model"],
                        single, both))
        if apply:
            cursor.execute(
                "UPDATE contact_lens_products SET min_boxes_single_eye = %s, "
                "min_boxes_both_per_eye = %s WHERE product_id = %s",
                (single, both, profile["product_id"]))
    return matched, unmatched
