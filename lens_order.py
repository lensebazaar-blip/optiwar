"""Ordering a contact lens: the matrix decides, per eye, in boxes.

A frame is one object with a quantity. A box of lenses is a prescription, an
eye, and a number of boxes — and the two eyes are independent, so a customer
orders two boxes of -4.50/-1.25x180 for the right eye and three boxes of -4.00
for the left in one line and is charged for five boxes.

Two rules are enforced here rather than in a template:

Presence of a variant row is the only thing that makes a combination orderable.
A range says nothing about the step the manufacturer makes, or about the holes
it leaves in the middle, so a selection is looked up, never arithmetic.

Availability is the lens's own. ``product_quantity`` counts frames in a drawer;
a box of lenses is IN_STOCK, or ON_ORDER with a lead time, and purchasable in
both — it is never sold out because a frame column says 0.
"""

VARIANTS_SQL = """
SELECT variant_id, sph, cyl, axis, add_power, base_curve, diameter,
       color_code, color_name
FROM contact_lens_variants
WHERE product_id = %s AND available = 1
ORDER BY color_code, sph, cyl, axis, add_power
"""

EYES = ("right", "left")

# What the customer chose, and what a variant row is matched on. ``color`` is a
# code rather than a name because names are display copy.
PARAMS = ("sph", "cyl", "axis", "add_power", "color")

MAX_BOXES_PER_EYE = 24

# Why an order was refused, as codes rather than sentences: the sentence is the
# customer's, the code is what the event stream and the daily report count. A
# rejection is recorded by code and product only — never with the prescription
# the customer typed.
REFUSED_BOXES = "BOXES_ABOVE_LIMIT"
REFUSED_NOT_MADE = "COMBINATION_NOT_MADE"
REFUSED_NO_BOXES = "NO_BOXES_CHOSEN"
REFUSED_NO_PRICE = "NO_PRICE"


def variants(cursor, product_id):
    """Every orderable combination for one lens."""
    cursor.execute(VARIANTS_SQL, (product_id,))
    return [dict(r) for r in (cursor.fetchall() or ())]


def _num(value):
    """A decimal parameter as a canonical string, or '' when it does not apply.

    Everything is compared as text: the database returns ``Decimal('-4.50')``,
    a form returns ``'-4.5'``, and a lens with no cylinder returns ``None``.
    """
    if value is None or value == "":
        return ""
    try:
        return "%.2f" % float(value)
    except (TypeError, ValueError):
        return ""


def _axis(value):
    if value is None or value == "":
        return ""
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return ""


def key(selection):
    """The signature a selection and a variant row are compared on."""
    return (_num(selection.get("sph")),
            _num(selection.get("cyl")),
            _axis(selection.get("axis")),
            _num(selection.get("add_power")),
            (selection.get("color") or selection.get("color_code")
             or "").strip())


def options(rows):
    """The choices a customer may make, derived from the rows themselves.

    Nested, because a cylinder is only offered for the spheres that have it and
    an axis only for that pair: a flat list of each parameter would offer
    combinations the manufacturer does not make.
    """
    colors, tree = [], {}
    seen = set()
    for row in rows:
        code = (row.get("color_code") or "").strip()
        if code and code not in seen:
            seen.add(code)
            colors.append({"code": code,
                           "name": row.get("color_name") or code})
        sph, cyl, axis, add = (_num(row.get("sph")), _num(row.get("cyl")),
                               _axis(row.get("axis")),
                               _num(row.get("add_power")))
        per_color = tree.setdefault(code, {})
        per_sph = per_color.setdefault(sph, {})
        per_cyl = per_sph.setdefault(cyl, {"axes": [], "adds": []})
        if axis and axis not in per_cyl["axes"]:
            per_cyl["axes"].append(axis)
        if add and add not in per_cyl["adds"]:
            per_cyl["adds"].append(add)
    return {"colors": colors, "tree": tree}


def find(rows, selection):
    """The variant a selection names, or ``None``."""
    wanted = key(selection)
    for row in rows:
        if key(row) == wanted:
            return row
    return None


def boxes(value):
    try:
        n = int(str(value or 0).strip() or 0)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def read_eye(form, eye):
    """One eye's selection out of a submitted form."""
    return {
        "eye": eye,
        "sph": (form.get("%s_sph" % eye) or "").strip(),
        "cyl": (form.get("%s_cyl" % eye) or "").strip(),
        "axis": (form.get("%s_axis" % eye) or "").strip(),
        "add_power": (form.get("%s_add" % eye) or "").strip(),
        "color": (form.get("%s_color" % eye) or "").strip(),
        "boxes": boxes(form.get("%s_boxes" % eye)),
    }


def box_price(product):
    """EUR per box: the offer price, and the list price only if there is none."""
    for field in ("product_special_price_eur", "product_price_eur"):
        try:
            price = float(product.get(field) or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price > 0:
            return round(price, 2)
    return 0.0


def validate_detailed(rows, product, selections):
    """Accept a per-eye order, or say why not.

    Returns ``(lines, problems)``. ``lines`` carry the matched variant and the
    boxes for each eye that was ordered; a non-empty ``problems`` means nothing
    should be added to a cart. Each problem is ``(code, sentence)`` so the same
    refusal can be shown to the customer and counted by its reason.
    """
    problems, lines = [], []
    for sel in selections:
        if not sel.get("boxes"):
            continue
        if sel["boxes"] > MAX_BOXES_PER_EYE:
            problems.append((REFUSED_BOXES, "%s eye: at most %d boxes per eye"
                             % (sel["eye"], MAX_BOXES_PER_EYE)))
            continue
        variant = find(rows, sel)
        if not variant:
            problems.append((REFUSED_NOT_MADE,
                             "%s eye: this combination is not made for this "
                             "lens" % sel["eye"]))
            continue
        lines.append({"eye": sel["eye"], "boxes": sel["boxes"],
                      "variant": variant})
    if not lines and not problems:
        problems.append((REFUSED_NO_BOXES,
                         "Choose the boxes for at least one eye"))
    if not box_price(product) and not problems:
        problems.append((REFUSED_NO_PRICE, "This lens has no price"))
    return lines, problems


def validate(rows, product, selections):
    """``validate_detailed`` for a caller that only shows the sentences."""
    lines, problems = validate_detailed(rows, product, selections)
    return lines, [message for _, message in problems]


def describe(variant, boxes_ordered):
    """A selection as one line of text, for a cart, an order and an email."""
    parts = []
    sph = _num(variant.get("sph"))
    if sph:
        parts.append("SPH %s" % ("PLANO" if float(sph) == 0 else sph))
    cyl = _num(variant.get("cyl"))
    if cyl:
        parts.append("CYL %s" % cyl)
    axis = _axis(variant.get("axis"))
    if axis:
        parts.append("AXIS %s" % axis)
    add = _num(variant.get("add_power"))
    if add:
        parts.append("ADD %s" % add)
    name = variant.get("color_name") or variant.get("color_code")
    if name:
        parts.append(str(name))
    parts.append("%d box%s" % (boxes_ordered,
                               "" if boxes_ordered == 1 else "es"))
    return " / ".join(parts)


def cart_item(product, lines):
    """The cart entry for a validated per-eye order.

    Priced here from the catalogue row: boxes × box price, so a posted price
    cannot decide what is charged. Written in the shape checkout, the order
    tables and the confirmation email already read for a lens line.
    """
    price = box_price(product)
    per_eye = {eye: next((ln for ln in lines if ln["eye"] == eye), None)
               for eye in EYES}
    item = {
        "product_id": str(product.get("product_id")),
        "product_code": product.get("product_code"),
        "product_name": product.get("product_name"),
        "product_category": "Contact Lenses",
        "product_special_price": price,
        "product_price": price,
        "full_product_price": price,
        "vertical": "CONTACT_LENS",
        "availability": (product.get("availability") or "").strip().upper(),
        "lead_time_days": product.get("lead_time_days"),
        "rx_id": None,
        "recommendations": product.get("product_name"),
    }
    total_boxes = 0
    for eye in EYES:
        line = per_eye[eye]
        variant = line["variant"] if line else {}
        count = line["boxes"] if line else 0
        total_boxes += count
        item.update({
            "%s_qty" % eye: count,
            "%s_pwr" % eye: _num(variant.get("sph")),
            "%s_cyl" % eye: _num(variant.get("cyl")),
            "%s_axis" % eye: _axis(variant.get("axis")),
            "%s_add" % eye: _num(variant.get("add_power")),
            "%s_lens_color" % eye: (variant.get("color_code") or ""),
            "%s_variant_id" % eye: variant.get("variant_id"),
            "%s_eye" % eye: (describe(variant, count) if count
                             else "No RX selected"),
        })
    item["order_quantity"] = total_boxes
    item["ATC_WCL"] = round(price * total_boxes, 2)
    item["total_savings"] = 0
    return item
