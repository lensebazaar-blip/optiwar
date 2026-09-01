"""Validating a contact-lens export before any of it becomes a product.

The rows come from another company's spreadsheet, so this module's whole job is
to refuse. It parses, normalises and checks; it never repairs, never infers a
missing manufacturer fact, and never expands a stated range into combinations —
sphere steps change across a range and axis availability differs by cylinder, so
the cross product of the minima and maxima is a list of powers a customer could
order and nobody could supply.

An export therefore arrives in one of two shapes per product, and says which in
``param_mode`` (see ``contact_lens.py``):

``MATRIX``  a combinations sheet — one row per orderable combination.
``RULES``   a parameter sheet — one row per selectable value of one parameter.
            A source that holds no combination data says this, and saying it is
            not the same as expanding it: the lists are stored as lists.

No database and no flask here on purpose: a rejection has to be provable in a
test without a box, and the CLI that writes (``scripts/import_contact_lenses.py``)
does nothing this module has not already approved.

    products, errors = parse(product_rows, variant_rows, rule_rows)

``products`` are whole and importable; ``errors`` name a row and what is wrong
with it. A product with any error is not in ``products`` at all — a product and
what may be ordered against it arrive together or not at all.
"""
import decimal

MODALITIES = ("DAILY", "MONTHLY", "CONVENTIONAL")
LENS_TYPES = ("SPHERICAL", "TORIC", "MULTIFOCAL", "TORIC_MULTIFOCAL", "COLOR")
AVAILABILITIES = ("IN_STOCK", "ON_ORDER")

SOURCE_SYSTEM = "lensbazaar"

PARAM_MODE_RULES = "RULES"
PARAM_MODE_MATRIX = "MATRIX"
PARAM_MODES = (PARAM_MODE_RULES, PARAM_MODE_MATRIX)

# The parameters a rules sheet may state, and how each value is written down.
RULE_PARAMETERS = ("base_curve", "diameter", "sph", "cyl", "axis", "add_power",
                   "color")

# What each lens type must be configured on when it is stated as rules. A toric
# with no axis list is not orderable; a spherical with a cylinder list is a
# sheet whose parameter column says the wrong thing.
TYPE_RULE_PARAMS = {
    "SPHERICAL": {"required": ("sph",), "forbidden": ("cyl", "axis",
                                                      "add_power")},
    "TORIC": {"required": ("sph", "cyl", "axis"), "forbidden": ("add_power",)},
    "MULTIFOCAL": {"required": ("sph", "add_power"), "forbidden": ("cyl",
                                                                   "axis")},
    "TORIC_MULTIFOCAL": {"required": ("sph", "cyl", "axis", "add_power"),
                         "forbidden": ()},
    "COLOR": {"required": ("sph", "color"), "forbidden": ("add_power",)},
}

# The manufacturers we publish, against the strings sources write them as. The
# source's own value is kept beside this one: "Johnsons and Johnsons" is
# provenance, and is never what a customer or Google is shown.
CANONICAL_MANUFACTURERS = {
    "ALCON": "Alcon",
    "COOPERVISION": "CooperVision",
    "COOPER_VISION": "CooperVision",
    "JOHNSON_&_JOHNSON": "Johnson & Johnson Vision",
    "JOHNSON_AND_JOHNSON": "Johnson & Johnson Vision",
    "JOHNSONS_AND_JOHNSONS": "Johnson & Johnson Vision",
    "JOHNSON_&_JOHNSON_VISION": "Johnson & Johnson Vision",
    "J&J": "Johnson & Johnson Vision",
    "ACUVUE": "Johnson & Johnson Vision",
    "BAUSCH_AND_LOMB": "Bausch + Lomb",
    "BAUSCH_+_LOMB": "Bausch + Lomb",
    "BAUSCH_&_LOMB": "Bausch + Lomb",
}


def canonical_manufacturer(raw):
    """The manufacturer as we publish it, or the source's own string.

    Unknown names pass through rather than being rejected: a manufacturer we
    have not met is a normalisation to add, not a reason to refuse a product.
    """
    return CANONICAL_MANUFACTURERS.get(_upper(raw), _text(raw))


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
        stated = decimal.Decimal(raw)
    except (decimal.InvalidOperation, ValueError):
        raise RowError("%s: %r is not a whole number" % (field, value))
    number = int(stated)
    if stated != number:
        # Truncating turns a stated minimum of 4.5 boxes into a permission to
        # order 4, which is the one direction a count must never move on its
        # own. Every caller here is a count of whole things.
        raise RowError("%s: %s is not a whole number" % (field, stated))
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
        "manufacturer": canonical_manufacturer(row.get("manufacturer")),
        "source_manufacturer": _text(row.get("manufacturer")),
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
        "param_mode": _upper(row.get("param_mode")) or PARAM_MODE_MATRIX,
        "param_source": _text(row.get("param_source")),
        "min_boxes_single_eye": whole(row.get("min_boxes_single_eye"),
                                      "min_boxes_single_eye"),
        "min_boxes_both_per_eye": whole(row.get("min_boxes_both_per_eye"),
                                        "min_boxes_both_per_eye"),
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
    if product["manufacturer_mpn"] and (
            product["manufacturer_mpn"].strip().lower()
            in (product["product_name"].strip().lower(),
                _text(row.get("source_ref")).strip().lower())):
        # A supplier's own SKU or the product's name in the MPN column is not a
        # manufacturer part number, and sending it would collide with whatever
        # product genuinely carries that code. No identifier is the honest
        # answer; the feed then says identifier_exists=false.
        raise RowError("manufacturer_mpn %r is the product's own name or SKU, "
                       "not a manufacturer part number — leave it empty"
                       % product["manufacturer_mpn"])
    if product["special_price_eur"] and (product["special_price_eur"]
                                         > product["price_eur"]):
        raise RowError("special_price_eur is above price_eur")
    if product["param_mode"] not in PARAM_MODES:
        raise RowError("param_mode: %r is not one of %s"
                       % (row.get("param_mode"), ", ".join(PARAM_MODES)))
    if product["param_mode"] == PARAM_MODE_RULES and not product["param_source"]:
        # Rules assert that every combination of the stated values is orderable.
        # Who asserted it has to be recorded, or nobody can later tell a
        # supplier's regional terms from a manufacturer's chart.
        raise RowError("param_mode RULES without param_source: an assertion "
                       "about what is orderable has to say whose it is")
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


def parse_rule(row):
    """One parameter-sheet row -> a selectable value of one parameter."""
    parameter = _text(row.get("parameter")).lower().replace(" ", "_")
    parameter = {"power": "sph", "pwr": "sph", "bc": "base_curve",
                 "dia": "diameter", "add": "add_power",
                 "colour": "color"}.get(parameter, parameter)
    if parameter not in RULE_PARAMETERS:
        raise RowError("parameter: %r is not one of %s"
                       % (row.get("parameter"), ", ".join(RULE_PARAMETERS)))
    raw = row.get("value")
    if parameter in ("sph", "cyl", "add_power"):
        value = dioptre(raw, parameter,
                        allow_zero=(parameter == "sph"))
    elif parameter == "axis":
        value = axis(raw)
    elif parameter in ("base_curve", "diameter"):
        value = millimetres(raw, parameter)
    else:
        value = _text(raw)
    if value is None or value == "":
        raise RowError("%s: no value" % parameter)
    if parameter == "cyl" and value > 0:
        raise RowError("cyl: %s is plus-form; the rules are minus-cylinder"
                       % value)
    return {
        "source_ref": _text(row.get("source_ref")),
        "parameter": parameter,
        "value": str(value),
        "label": _text(row.get("label")) or None,
        "available": 0 if _text(row.get("available")).lower() in (
            "0", "no", "false", "discontinued") else 1,
    }


def _check_rule_shape(product):
    """Whether a rules product states the parameters its type is chosen on."""
    stated = {r["parameter"] for r in product["rules"] if r["available"]}
    rules = TYPE_RULE_PARAMS[product["lens_type"]]
    problems = ["%s lens with no %s values" % (product["lens_type"], p)
                for p in rules["required"] if p not in stated]
    problems += ["%s lens with %s values" % (product["lens_type"], p)
                 for p in rules["forbidden"] if p in stated]
    # Diameter is not asked for anywhere — it is filled in from the one the
    # product is made in. Two of them is a choice the customer would never be
    # shown and we would make for them, so it is refused rather than picked.
    diameters = {r["value"] for r in product["rules"]
                 if r["available"] and r["parameter"] == "diameter"}
    if len(diameters) > 1:
        problems.append("%d diameter values: a lens is ordered in one, and "
                        "the customer is not asked which"
                        % len(diameters))
    return problems


def parse(product_rows, variant_rows, rule_rows=()):
    """Validate the export. Returns (products, errors).

    A product is returned only with what may be ordered against it, and only
    when nothing about it or any of its rows was rejected, because a lens whose
    matrix half-loaded would sell the half that loaded.
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
        product["rules"] = []
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

    seen_rules = {}
    for number, row in enumerate(rule_rows or (), start=2):
        ref = _text(row.get("source_ref"))
        product = products.get(ref)
        if product is None:
            errors.append(("rules", number, ref,
                           "no product row with this source_ref"))
            continue
        try:
            rule = parse_rule(row)
        except RowError as exc:
            errors.append(("rules", number, ref, str(exc)))
            continue
        signature = (rule["parameter"], rule["value"])
        if signature in seen_rules.setdefault(ref, set()):
            errors.append(("rules", number, ref, "duplicate %s value %s"
                           % signature))
            continue
        seen_rules[ref].add(signature)
        product["rules"].append(rule)

    rejected |= {ref for _sheet, _n, ref, _why in errors if ref}
    for ref, product in products.items():
        if product["param_mode"] == PARAM_MODE_RULES:
            if product["variants"]:
                # Both shapes for one lens would leave two answers to the same
                # question, and no way to tell which one served a customer.
                errors.append(("variants", 0, ref,
                               "param_mode RULES with combination rows"))
                rejected.add(ref)
            for problem in _check_rule_shape(product):
                errors.append(("rules", 0, ref, problem))
                rejected.add(ref)
            if not any(r["available"] for r in product["rules"]):
                errors.append(("rules", 0, ref,
                               "no available values: nothing to sell"))
                rejected.add(ref)
            continue
        if product["rules"]:
            errors.append(("rules", 0, ref,
                           "param_mode MATRIX with parameter rows"))
            rejected.add(ref)
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
        rules = product.get("param_mode") == PARAM_MODE_RULES
        stated = ("%d stated value(s) in %d parameter(s)"
                  % (len(product["rules"]),
                     len({r["parameter"] for r in product["rules"]}))
                  if rules else "%d combination(s)" % len(product["variants"]))
        lines.append("  OK   %-14s %-14s %-28s %s %s pack=%s  %s  %s"
                     % (product["source_ref"], product["manufacturer"],
                        product["product_name"][:28], product["modality"],
                        product["lens_type"], product["pack_quantity"],
                        product.get("param_mode", ""), stated))
    for sheet, number, ref, why in errors:
        where = "%s row %s" % (sheet, number) if number else sheet
        lines.append("  REJECT %-14s %-18s %s" % (ref or "-", where, why))
    return "\n".join(lines)
