"""Validating a contact-lens export before any of it becomes a product.

The rows come from another company's spreadsheet, so this module's whole job is
to refuse. It parses, normalises and checks; it never repairs, never infers a
missing manufacturer fact, and never expands a stated range into combinations —
sphere steps change across a range and axis availability differs by cylinder, so
the cross product of the minima and maxima is a list of powers a customer could
order and nobody could supply.

No database and no flask here on purpose: a rejection has to be provable in a
test without a box, and the CLI that writes (``scripts/import_contact_lenses.py``)
does nothing this module has not already approved.

    products, errors = parse(product_rows, variant_rows)

``products`` are whole and importable; ``errors`` name a row and what is wrong
with it. A product with any error is not in ``products`` at all — a product and
its matrix arrive together or not at all.
"""
import decimal

MODALITIES = ("DAILY", "MONTHLY", "CONVENTIONAL")
LENS_TYPES = ("SPHERICAL", "TORIC", "MULTIFOCAL", "TORIC_MULTIFOCAL", "COLOR")
AVAILABILITIES = ("IN_STOCK", "ON_ORDER")

SOURCE_SYSTEM = "lensbazaar"

# Quarter-dioptre is the step every manufacturer prescribes in. A value off the
# step is a transcription error in the export, not an exotic lens.
STEP = decimal.Decimal("0.25")

PRODUCT_REQUIRED = ("source_ref", "manufacturer", "brand", "product_name",
                    "modality", "lens_type", "pack_quantity", "price_eur",
                    "availability", "image_url")

# What each lens type must and must not state per variant. A toric lens with no
# axis is not a lens anybody can order, and a spherical lens with a cylinder is
# a row that landed in the wrong column.
TYPE_RULES = {
    "SPHERICAL": {"required": (), "forbidden": ("cyl", "axis", "add_power")},
    "TORIC": {"required": ("cyl", "axis"), "forbidden": ("add_power",)},
    "MULTIFOCAL": {"required": ("add_power",), "forbidden": ("cyl", "axis")},
    "TORIC_MULTIFOCAL": {"required": ("cyl", "axis", "add_power"),
                         "forbidden": ()},
    "COLOR": {"required": (), "forbidden": ("add_power",)},
}


class RowError(Exception):
    """One row, one reason. Collected rather than raised at the caller."""


def _text(value):
    return ("" if value is None else str(value)).strip()


def _upper(value):
    return _text(value).upper().replace(" ", "_").replace("-", "_")


def dioptre(value, field, allow_zero=True):
    """A power, as a quarter-step Decimal, or None when the cell is empty.

    Empty means "this parameter does not apply to this lens", which is why the
    column is nullable: a spherical lens has no cylinder, and 0.00 would say it
    has a cylinder of zero.
    """
    raw = _text(value)
    if raw == "":
        return None
    raw = raw.replace("+", "")
    try:
        power = decimal.Decimal(raw)
    except decimal.InvalidOperation:
        raise RowError("%s: %r is not a number" % (field, value))
    if abs(power) > 30:
        raise RowError("%s: %s is outside any prescription" % (field, power))
    if power % STEP != 0:
        raise RowError("%s: %s is not a quarter-dioptre step" % (field, power))
    if not allow_zero and power == 0:
        raise RowError("%s: 0.00 is not a value this parameter takes" % field)
    return power.quantize(decimal.Decimal("0.01"))


def axis(value):
    raw = _text(value)
    if raw == "":
        return None
    try:
        deg = int(decimal.Decimal(raw))
    except (decimal.InvalidOperation, ValueError):
        raise RowError("axis: %r is not a whole number of degrees" % value)
    if not 0 <= deg <= 180:
        raise RowError("axis: %s is outside 0-180" % deg)
    return deg


def millimetres(value, field):
    raw = _text(value)
    if raw == "":
        return None
    try:
        mm = decimal.Decimal(raw)
    except decimal.InvalidOperation:
        raise RowError("%s: %r is not a measurement" % (field, value))
    if not decimal.Decimal("5") <= mm <= decimal.Decimal("20"):
        raise RowError("%s: %s mm is not a contact lens" % (field, mm))
    return mm.quantize(decimal.Decimal("0.01"))


def money(value, field):
    raw = _text(value).replace("\u20ac", "").replace(",", "")
    if raw == "":
        return None
    try:
        amount = decimal.Decimal(raw)
    except decimal.InvalidOperation:
        raise RowError("%s: %r is not a price" % (field, value))
    if amount <= 0:
        raise RowError("%s: %s is not a price" % (field, amount))
    return amount.quantize(decimal.Decimal("0.01"))


def whole(value, field):
    raw = _text(value)
    if raw == "":
        return None
    try:
        number = int(decimal.Decimal(raw))
    except (decimal.InvalidOperation, ValueError):
        raise RowError("%s: %r is not a whole number" % (field, value))
    if number <= 0:
        raise RowError("%s: %s is not a count" % (field, number))
    return number


def variant_signature(variant):
    """The same identity ``contact_lens_variants.variant_sig`` computes.

    Duplicated here so a duplicate is reported against a spreadsheet row the ops
    team can find, instead of surfacing as a unique-key violation mid-import.
    """
    def cell(key):
        value = variant.get(key)
        return "NA" if value is None else str(value)
    return "|".join([cell("sph"), cell("cyl"), cell("axis"),
                     cell("add_power"), cell("base_curve"), cell("diameter"),
                     variant.get("color_code") or ""])


def parse_product(row):
    """One product row -> the fields the profile and product tables want."""
    product = {
        "source_system": SOURCE_SYSTEM,
        "source_ref": _text(row.get("source_ref")),
        "manufacturer": _text(row.get("manufacturer")),
        "brand": _text(row.get("brand")),
        "product_name": _text(row.get("product_name")),
        "gtin": _text(row.get("gtin")),
        "manufacturer_mpn": _text(row.get("manufacturer_mpn")),
        "modality": _upper(row.get("modality")),
        "lens_type": _upper(row.get("lens_type")),
        "pack_quantity": whole(row.get("pack_quantity"), "pack_quantity"),
        "material": _text(row.get("material")),
        "water_content": _percent(row.get("water_content")),
        "replacement_days": whole(row.get("replacement_days"),
                                  "replacement_days"),
        "availability": _upper(row.get("availability")) or "IN_STOCK",
        "lead_time_days": whole(row.get("lead_time_days"), "lead_time_days"),
        "price_eur": money(row.get("price_eur"), "price_eur"),
        "special_price_eur": money(row.get("special_price_eur"),
                                   "special_price_eur"),
        "image_url": _text(row.get("image_url")),
        "product_details": _text(row.get("description")),
        "source_url": _text(row.get("source_url")),
    }
    missing = [f for f in PRODUCT_REQUIRED if not product.get(f)]
    if missing:
        raise RowError("missing required field(s): %s" % ", ".join(missing))
    if product["modality"] not in MODALITIES:
        raise RowError("modality: %r is not one of %s"
                       % (row.get("modality"), ", ".join(MODALITIES)))
    if product["lens_type"] not in LENS_TYPES:
        raise RowError("lens_type: %r is not one of %s"
                       % (row.get("lens_type"), ", ".join(LENS_TYPES)))
    if product["availability"] not in AVAILABILITIES:
        # OUT_OF_STOCK is deliberately not accepted: a lens is replenished, so
        # it is IN_STOCK or ON_ORDER, and never removed from sale by a quantity.
        raise RowError("availability: %r is not one of %s"
                       % (row.get("availability"), ", ".join(AVAILABILITIES)))
    if product["availability"] == "ON_ORDER" and not product["lead_time_days"]:
        raise RowError("ON_ORDER without lead_time_days: a customer told to "
                       "wait has to be told how long")
    if not (product["gtin"] or product["manufacturer_mpn"]):
        raise RowError("neither GTIN nor manufacturer_mpn: the offer would "
                       "have to claim our product_code as the manufacturer's")
    if product["special_price_eur"] and (product["special_price_eur"]
                                         > product["price_eur"]):
        raise RowError("special_price_eur is above price_eur")
    return product


def _percent(raw):
    value = money(raw, "water_content")
    if value is not None and value > 100:
        raise RowError("water_content: %s%% is not a fraction of water"
                       % value)
    return value


def parse_variant(row, lens_type):
    variant = {
        "source_ref": _text(row.get("source_ref")),
        "sph": dioptre(row.get("sph"), "sph"),
        "cyl": dioptre(row.get("cyl"), "cyl", allow_zero=False),
        "axis": axis(row.get("axis")),
        "add_power": dioptre(row.get("add_power"), "add_power",
                             allow_zero=False),
        "base_curve": millimetres(row.get("base_curve"), "base_curve"),
        "diameter": millimetres(row.get("diameter"), "diameter"),
        "color_code": _text(row.get("color_code")),
        "color_name": _text(row.get("color_name")),
        "available": 0 if _text(row.get("available")).lower() in (
            "0", "no", "false", "discontinued") else 1,
    }
    if variant["sph"] is None:
        raise RowError("sph: every orderable lens has a sphere power, "
                       "including plano (0.00)")
    rules = TYPE_RULES[lens_type]
    for field in rules["required"]:
        if variant.get(field) is None:
            raise RowError("%s lens with no %s" % (lens_type, field))
    for field in rules["forbidden"]:
        if variant.get(field) is not None:
            raise RowError("%s lens with a %s" % (lens_type, field))
    if variant["cyl"] is not None and variant["cyl"] > 0:
        # Manufacturers state cylinder in minus form. A positive value means the
        # export transposed a sign, and a transposed sign is the wrong lens.
        raise RowError("cyl: %s is plus-form; the matrix is minus-cylinder"
                       % variant["cyl"])
    if lens_type == "COLOR" and not variant["color_code"]:
        raise RowError("COLOR lens with no color_code")
    if lens_type != "COLOR" and variant["color_code"]:
        raise RowError("color_code on a %s lens" % lens_type)
    return variant


def parse(product_rows, variant_rows):
    """Validate the export. Returns (products, errors).

    A product is returned only with a matrix, and only when nothing about it or
    any of its variants was rejected, because a lens whose matrix half-loaded
    would sell the half that loaded.
    """
    errors = []
    products = {}
    rejected = set()
    for number, row in enumerate(product_rows, start=2):
        try:
            product = parse_product(row)
        except RowError as exc:
            errors.append(("products", number, _text(row.get("source_ref")),
                           str(exc)))
            continue
        if product["source_ref"] in products:
            errors.append(("products", number, product["source_ref"],
                           "source_ref appears twice in the export"))
            continue
        product["variants"] = []
        products[product["source_ref"]] = product

    seen_gtin = {}
    for ref, product in products.items():
        gtin = product["gtin"]
        if not gtin:
            continue
        if gtin in seen_gtin:
            # Neither is importable: an identifier two products claim does not
            # identify either of them, and the export has to say which is which.
            errors.append(("products", 0, ref,
                           "GTIN %s is also claimed by %s"
                           % (gtin, seen_gtin[gtin])))
            rejected.update((ref, seen_gtin[gtin]))
        else:
            seen_gtin[gtin] = ref

    signatures = {}
    for number, row in enumerate(variant_rows, start=2):
        ref = _text(row.get("source_ref"))
        product = products.get(ref)
        if product is None:
            errors.append(("variants", number, ref,
                           "no product row with this source_ref"))
            continue
        try:
            variant = parse_variant(row, product["lens_type"])
        except RowError as exc:
            errors.append(("variants", number, ref, str(exc)))
            continue
        signature = variant_signature(variant)
        if signature in signatures.setdefault(ref, set()):
            errors.append(("variants", number, ref,
                           "duplicate combination %s" % signature))
            continue
        signatures[ref].add(signature)
        product["variants"].append(variant)

    rejected |= {ref for _sheet, _n, ref, _why in errors if ref}
    for ref, product in products.items():
        if not any(v["available"] for v in product["variants"]):
            errors.append(("variants", 0, ref,
                           "no available combination: nothing to sell"))
            rejected.add(ref)

    importable = [p for ref, p in sorted(products.items())
                  if ref not in rejected]
    return importable, errors


def report(products, errors):
    """The dry run's text. Every rejection names a sheet, a row and a reason."""
    lines = ["%d product(s) importable, %d rejection(s)"
             % (len(products), len(errors))]
    for product in products:
        lines.append("  OK   %-14s %-14s %-28s %s %s pack=%s  %d variant(s)"
                     % (product["source_ref"], product["manufacturer"],
                        product["product_name"][:28], product["modality"],
                        product["lens_type"], product["pack_quantity"],
                        len(product["variants"])))
    for sheet, number, ref, why in errors:
        where = "%s row %s" % (sheet, number) if number else sheet
        lines.append("  REJECT %-14s %-18s %s" % (ref or "-", where, why))
    return "\n".join(lines)
