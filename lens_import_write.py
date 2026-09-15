"""Writing a validated contact lens: the one place the tables are touched.

``cl_import.parse()`` decides what may be written; this module writes it, for
the CLI (``scripts/import_contact_lenses.py``) and the Ops console
(``lens_import.py``) alike, so a lens imported from a terminal and one
confirmed in a browser land in the database the same way.

Per product, inside the caller's transaction:

    products                    the commercial record, CONTACT_LENS, .com only
    contact_lens_products       brand, manufacturer, modality, pack, minimums,
                                the reference GTIN's power, the legacy id
    contact_lens_param_rules    the stated values, when param_mode is RULES
    contact_lens_variants       the matrix, when param_mode is MATRIX
    contact_lens_images         the primary image, or every approved view of
                                the product's image recipe
    contact_lens_aliases        ALSO_KNOWN_AS rows for an official former name

Idempotent on (source_system, source_ref). Nothing is deleted: a combination
or value the source no longer states becomes ``available = 0``. And
``merchant_enabled`` is never written here — a lens arrives hidden and stays
hidden until somebody releases it (``lens_import.release``).
"""
import datetime

try:
    from . import cl_import, contact_lens, lens_identity
except ImportError:
    import cl_import
    import contact_lens
    import lens_identity

# The .com-only launch. Site eligibility lives on products because one function
# in catalogue.py decides it for every vertical and every surface.
SELL_ON = {"sell_on_com": 1, "sell_on_in": 0}


def existing(cursor, product):
    cursor.execute("SELECT product_id FROM contact_lens_products"
                   " WHERE source_system = %s AND source_ref = %s",
                   (product["source_system"], product["source_ref"]))
    row = cursor.fetchone()
    return row["product_id"] if row else None


def product_code(product):
    """Our internal offer id. Never sent as the manufacturer's identifier."""
    return ("CL-" + product["source_ref"].upper())[:20]


def slug(product):
    """Optiwar's own slug, from the canonical name we sell the lens under."""
    name = product.get("canonical_name") or product["product_name"]
    text = name if name.lower().startswith(product["brand"].lower()) else (
        "%s %s" % (product["brand"], name))
    keep = [c.lower() if c.isalnum() else "-" for c in text]
    return "-".join("".join(keep).split("-")[:12]).strip("-")[:180]


def in_rupees(amount, rate):
    """EUR -> INR at a stated rate, or None when no rate was supplied.

    EUR is canonical. A rupee price is derived from it once, at a rate the run
    was given and records; it is never re-derived from a previous conversion,
    which is how a price drifts every time somebody re-imports.
    """
    if not rate or amount in (None, ""):
        return None
    # products.product_price is whole rupees, so the rounding is done here
    # where it is stated rather than by the column on the way in.
    return int(round(float(amount) * float(rate)))


def upsert_product(cursor, product, product_id, rate=None):
    fields = {
        "product_code": product_code(product),
        "product_name": product.get("canonical_name") or product["product_name"],
        "product_details": product["product_details"],
        "product_price_eur": product["price_eur"],
        "product_special_price_eur": product["special_price_eur"],
        "product_image": product["image_url"],
        "product_slug": slug(product),
        "product_vertical": contact_lens.VERTICAL,
        "product_status": "ACTIVE",
    }
    fields.update(SELL_ON)
    rupees = in_rupees(product["price_eur"], rate)
    if rupees is not None:
        fields["product_price"] = rupees
        fields["product_special_price"] = in_rupees(
            product["special_price_eur"] or product["price_eur"], rate)
    if product_id:
        assignments = ", ".join("%s = %%s" % k for k in fields)
        cursor.execute("UPDATE products SET %s WHERE product_id = %%s"
                       % assignments,
                       tuple(fields.values()) + (product_id,))
        return product_id
    columns = ", ".join(fields)
    marks = ", ".join(["%s"] * len(fields))
    cursor.execute("INSERT INTO products (%s) VALUES (%s)" % (columns, marks),
                   tuple(fields.values()))
    return cursor.lastrowid


def upsert_profile(cursor, product, product_id, rate=None):
    now = datetime.datetime.now()
    fields = {
        "product_id": product_id,
        "brand": product["brand"],
        "manufacturer": product["manufacturer"],
        "source_manufacturer": product["source_manufacturer"] or None,
        "param_mode": product["param_mode"],
        "param_source": product["param_source"] or None,
        "min_boxes_single_eye": product["min_boxes_single_eye"],
        "min_boxes_both_per_eye": product["min_boxes_both_per_eye"],
        "min_order_model": product["min_order_model"] or None,
        "gtin": product["gtin"] or None,
        "gtin_reference_power": product.get("gtin_reference_power") or None,
        "manufacturer_mpn": product["manufacturer_mpn"] or None,
        "canonical_name": product.get("canonical_name") or None,
        "legacy_ref_id": product.get("legacy_ref_id") or None,
        "ships_within_text": product.get("ships_within_text") or None,
        "modality": product["modality"],
        "lens_type": product["lens_type"],
        "pack_quantity": product["pack_quantity"],
        "material": product["material"] or None,
        "water_content": product["water_content"],
        "replacement_days": product["replacement_days"],
        "availability": product["availability"],
        "lead_time_days": product["lead_time_days"],
        "source_system": product["source_system"],
        "source_ref": product["source_ref"],
        "imported_at": now,
        "eur_inr_rate": rate,
        "eur_inr_rate_at": now if rate else None,
    }
    columns = ", ".join(fields)
    marks = ", ".join(["%s"] * len(fields))
    # merchant_enabled is absent on purpose: an update must not re-release a
    # lens somebody withdrew, and an insert takes the column's default of 0.
    # The conversion metadata is held back for the same reason upsert_product
    # leaves the rupee prices alone when no rate was given: the recorded rate
    # describes the rupee price that is still there, and blanking it would
    # leave a converted price nothing accounts for.
    kept = {"product_id"} if rate else {"product_id", "eur_inr_rate",
                                        "eur_inr_rate_at"}
    updates = ", ".join("%s = VALUES(%s)" % (k, k) for k in fields
                        if k not in kept)
    cursor.execute("INSERT INTO contact_lens_products (%s) VALUES (%s)"
                   " ON DUPLICATE KEY UPDATE %s" % (columns, marks, updates),
                   tuple(fields.values()))


def upsert_aliases(cursor, product, product_id):
    """The official former names, as ALSO_KNOWN_AS rows; duplicates ignored."""
    written = 0
    for name in product.get("also_known_as") or ():
        if lens_identity.add_alias(
                cursor, product_id, lens_identity.ALIAS_ALSO_KNOWN_AS, name,
                source_type=lens_identity.SOURCE_SUPPLIER_LIST,
                source_ref=product.get("param_source") or product["source_system"]):
            written += 1
    return written


def upsert_variants(cursor, product, product_id):
    """Upsert every stated combination; withdraw the ones no longer stated.

    Withdrawal is ``available = 0`` rather than a DELETE, because an order line
    that pointed at a combination must remain readable after the manufacturer
    stops making it.
    """
    stated = set()
    for variant in product["variants"]:
        cursor.execute(
            "INSERT INTO contact_lens_variants (product_id, sph, cyl, axis,"
            " add_power, base_curve, diameter, color_code, color_name,"
            " available) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON DUPLICATE KEY UPDATE color_name = VALUES(color_name),"
            " available = VALUES(available)",
            (product_id, variant["sph"], variant["cyl"], variant["axis"],
             variant["add_power"], variant["base_curve"], variant["diameter"],
             variant["color_code"], variant["color_name"] or None,
             variant["available"]))
        stated.add(cl_import.variant_signature(variant))
    cursor.execute("SELECT variant_id, variant_sig FROM contact_lens_variants"
                   " WHERE product_id = %s AND available = 1", (product_id,))
    withdrawn = [r["variant_id"] for r in (cursor.fetchall() or ())
                 if r["variant_sig"] not in stated]
    for variant_id in withdrawn:
        cursor.execute("UPDATE contact_lens_variants SET available = 0"
                       " WHERE variant_id = %s", (variant_id,))
    return len(stated), len(withdrawn)


def upsert_rules(cursor, product, product_id):
    """Upsert every stated value; withdraw the ones no longer stated.

    Withdrawal is ``available = 0`` for the same reason a combination's is: an
    order that was placed on a power the supplier has dropped stays readable.
    """
    stated = set()
    for order, rule in enumerate(product["rules"]):
        cursor.execute(
            "INSERT INTO contact_lens_param_rules (product_id, parameter,"
            " value, label, sort_order, available)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON DUPLICATE KEY UPDATE label = VALUES(label),"
            " sort_order = VALUES(sort_order), available = VALUES(available)",
            (product_id, rule["parameter"], rule["value"], rule["label"],
             order, rule["available"]))
        stated.add((rule["parameter"], rule["value"]))
    cursor.execute("SELECT rule_id, parameter, value FROM"
                   " contact_lens_param_rules"
                   " WHERE product_id = %s AND available = 1", (product_id,))
    withdrawn = [r["rule_id"] for r in (cursor.fetchall() or ())
                 if (r["parameter"], r["value"]) not in stated]
    for rule_id in withdrawn:
        cursor.execute("UPDATE contact_lens_param_rules SET available = 0"
                       " WHERE rule_id = %s", (rule_id,))
    return len(stated), len(withdrawn)


def upsert_image(cursor, product, product_id):
    cursor.execute("SELECT image_id FROM contact_lens_images"
                   " WHERE product_id = %s AND image_url = %s",
                   (product_id, product["image_url"]))
    if cursor.fetchone():
        return
    cursor.execute("INSERT INTO contact_lens_images (product_id, color_code,"
                   " image_url, image_type, sort_order)"
                   " VALUES (%s, NULL, %s, 'PRIMARY', 0)",
                   (product_id, product["image_url"]))


def upsert_views(cursor, records, product_id):
    """One row per approved view of the recipe, keyed on the view code.

    ``records`` is ``image_pipeline.image_records(recipe)``. A row is matched
    by ``view_code`` or, for imagery loaded before views were recorded, by
    ``image_url``, and updated in place; nothing is deleted. The recipe is the
    only source of what the images are, so the gallery, the feed and the
    sitemap cannot disagree with what was photographed. A view the recipe no
    longer names is marked WITHDRAWN, which every reader skips, so a withdrawn
    photograph stops being published without a row being lost.
    """
    written = 0
    codes = [r["code"] for r in records]
    cursor.execute("UPDATE contact_lens_images SET image_type = 'WITHDRAWN',"
                   " gmc_eligible = 0 WHERE product_id = %%s AND (color_code"
                   " IS NULL OR color_code = '') AND view_code IS NOT NULL"
                   " AND view_code NOT IN (%s)"
                   % ", ".join(["%s"] * len(codes)),
                   (product_id,) + tuple(codes))
    for record in records:
        fields = {
            "image_url": record["path"],
            "image_type": "PRIMARY" if record["is_primary"] else "GALLERY",
            "sort_order": record["position"],
            "view_code": record["code"],
            "view_name": record["view"],
            "alt_text": record["alt"],
            "gmc_eligible": 1 if record["gmc"] else 0,
        }
        cursor.execute("SELECT image_id FROM contact_lens_images"
                       " WHERE product_id = %s AND (color_code IS NULL OR"
                       " color_code = '') AND (view_code = %s OR"
                       " image_url = %s) ORDER BY image_id LIMIT 1",
                       (product_id, record["code"], record["path"]))
        row = cursor.fetchone()
        if row:
            assignments = ", ".join("%s = %%s" % k for k in fields)
            cursor.execute("UPDATE contact_lens_images SET %s"
                           " WHERE image_id = %%s" % assignments,
                           tuple(fields.values()) + (row["image_id"],))
        else:
            columns = ", ".join(["product_id", "color_code"] + list(fields))
            marks = ", ".join(["%s", "NULL"] + ["%s"] * len(fields))
            cursor.execute("INSERT INTO contact_lens_images (%s) VALUES (%s)"
                           % (columns, marks),
                           (product_id,) + tuple(fields.values()))
        written += 1
    return written


def primary_path(records):
    """The recipe's primary view path, or None when it names none."""
    return next((r["path"] for r in records if r["is_primary"]), None)


def withdraw_all(cursor, table, product_id):
    """Withdraw whatever the shape a product no longer uses still offers.

    A lens states what may be ordered in one shape or the other. If it changes
    shape, the rows of the shape it left are still marked available, and the
    storefront would have two answers to the same question. They are withdrawn,
    not deleted, so an order placed against one stays readable.
    """
    cursor.execute("UPDATE %s SET available = 0"
                   " WHERE product_id = %%s AND available = 1" % table,
                   (product_id,))
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def import_one(cursor, product, rate=None, records=None, write_variants=True):
    """One product and everything it states, inside the caller's transaction.

    ``records`` are the approved image views (``image_pipeline.image_records``);
    without them the product's ``image_url`` becomes the single PRIMARY row.
    ``write_variants=False`` leaves the matrix to a caller that publishes it
    from a compiled rule set (``lens_rules.compile_and_publish``), which
    carries the fulfilment tier every row belongs to.
    """
    product_id = existing(cursor, product)
    product_id = upsert_product(cursor, product, product_id, rate)
    upsert_profile(cursor, product, product_id, rate)
    upsert_aliases(cursor, product, product_id)
    written = withdrawn = 0
    if product["param_mode"] == cl_import.PARAM_MODE_RULES:
        written, withdrawn = upsert_rules(cursor, product, product_id)
        withdrawn += withdraw_all(cursor, "contact_lens_variants", product_id)
    elif write_variants:
        written, withdrawn = upsert_variants(cursor, product, product_id)
        withdrawn += withdraw_all(cursor, "contact_lens_param_rules",
                                  product_id)
    if records:
        upsert_views(cursor, records, product_id)
    else:
        upsert_image(cursor, product, product_id)
    return product_id, written, withdrawn
