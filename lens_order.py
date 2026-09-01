"""Ordering a contact lens: what is stated decides, per eye, in boxes.

A frame is one object with a quantity. A box of lenses is a prescription, an
eye, and a number of boxes — and the two eyes are independent, so a customer
orders two boxes of -4.50/-1.25x180 for the right eye and three boxes of -4.00
for the left in one line and is charged for five boxes.

What a customer may choose comes from the catalogue in one of two shapes, which
``contact_lens_products.param_mode`` names (see ``contact_lens.py``):

``MATRIX``  one row per orderable combination. A cylinder is offered only under
            a sphere that has it, an axis only under the pair that has it.
``RULES``   one row per selectable value of one parameter. Every combination of
            the stated values is orderable — which is what a source holding no
            combination data actually asserts, said honestly instead of being
            materialised into rows that would claim to be a manufacturer chart.

``selectable()`` returns the one the lens is stated in and both answer the same
two questions, so nothing above this line knows which shape it is talking to. A
selection is looked up either way, never computed: a range says nothing about
the step the manufacturer makes, or about the holes it leaves in the middle.

Three further rules are enforced here rather than in a template:

Base curve is a parameter like any other. A lens made in 8.5 and 9.0 is one
product with a choice, and the choice must be matched — but where only one value
is stated the customer is not asked, and it is filled in.

Minimum boxes are the product's own and the storefront's: LensBazaar's supply
terms make Acuvue Moist eight boxes for one eye or four per eye for both, and
those minimums apply on .com and not on .in.

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

RULES_SQL = """
SELECT parameter, value, label
FROM contact_lens_param_rules
WHERE product_id = %s AND available = 1
ORDER BY parameter, sort_order, value
"""

EYES = ("right", "left")

# What the customer chose, and what a stated value is matched on. ``color`` is a
# code rather than a name because names are display copy.
PARAMS = ("base_curve", "sph", "cyl", "axis", "add_power", "color")

# Which parameters each lens type is configured on. A toric with no axis is not
# orderable; a spherical with a cylinder is somebody's wrong form field.
TYPE_PARAMS = {
    "SPHERICAL": ("sph",),
    "TORIC": ("sph", "cyl", "axis"),
    "MULTIFOCAL": ("sph", "add_power"),
    "TORIC_MULTIFOCAL": ("sph", "cyl", "axis", "add_power"),
    "COLOR": ("sph", "color"),
}

MAX_BOXES_PER_EYE = 24

# Why an order was refused, as codes rather than sentences: the sentence is the
# customer's, the code is what the event stream and the daily report count. A
# rejection is recorded by code and product only — never with the prescription
# the customer typed.
REFUSED_BOXES = "BOXES_ABOVE_LIMIT"
REFUSED_NOT_MADE = "COMBINATION_NOT_MADE"
REFUSED_NO_BOXES = "NO_BOXES_CHOSEN"
REFUSED_NO_PRICE = "NO_PRICE"
REFUSED_MINIMUM = "BELOW_MINIMUM_BOXES"


def variants(cursor, product_id):
    """Every stated combination for one lens (MATRIX shape)."""
    cursor.execute(VARIANTS_SQL, (product_id,))
    return [dict(r) for r in (cursor.fetchall() or ())]


def param_rules(cursor, product_id):
    """Every stated value of every parameter for one lens (RULES shape)."""
    cursor.execute(RULES_SQL, (product_id,))
    lists = {}
    for row in (cursor.fetchall() or ()):
        row = dict(row)
        lists.setdefault(row["parameter"], []).append(
            {"value": _canonical(row["parameter"], row["value"]),
             "label": row.get("label") or str(row["value"])})
    return lists


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


def _code(value):
    return ("" if value is None else str(value)).strip()


# How each parameter is written down, so a rule, a variant row and a posted form
# field are the same string when they mean the same thing.
_CANONICAL = {
    "base_curve": _num, "diameter": _num, "sph": _num, "cyl": _num,
    "add_power": _num, "axis": _axis, "color": _code, "color_code": _code,
}


def _canonical(param, value):
    return _CANONICAL.get(param, _code)(value)


def _selected(selection, param):
    """One parameter out of a selection, canonically. ``color`` has two names."""
    if param == "color":
        return _code(selection.get("color") or selection.get("color_code"))
    return _canonical(param, selection.get(param))


def key(selection):
    """The signature a selection and a stated combination are compared on."""
    return tuple(_selected(selection, param) for param in PARAMS)


class Matrix(object):
    """What is orderable, stated one combination at a time."""

    mode = "MATRIX"

    def __init__(self, rows):
        self.rows = list(rows or ())

    def values(self, param):
        """Every value of one parameter that appears anywhere in the rows."""
        seen = []
        for row in self.rows:
            value = _selected(row, param)
            if value and value not in seen:
                seen.append(value)
        return seen

    def find(self, selection):
        wanted = key(selection)
        for row in self.rows:
            if key(row) == wanted:
                return dict(row)
        return None

    def options(self):
        """The choices a customer may make, derived from the rows themselves.

        Nested, because a cylinder is only offered for the spheres that have it
        and an axis only for that pair: a flat list of each parameter would
        offer combinations the manufacturer does not make.
        """
        colors, tree, curves = [], {}, self.values("base_curve")
        seen = set()
        for row in self.rows:
            code = _selected(row, "color")
            if code and code not in seen:
                seen.add(code)
                colors.append({"code": code,
                               "name": row.get("color_name") or code})
            per_color = tree.setdefault(code, {})
            per_bc = per_color.setdefault(_selected(row, "base_curve"), {})
            per_sph = per_bc.setdefault(_selected(row, "sph"), {})
            per_cyl = per_sph.setdefault(_selected(row, "cyl"),
                                         {"axes": [], "adds": []})
            axis_value = _selected(row, "axis")
            add_value = _selected(row, "add_power")
            if axis_value and axis_value not in per_cyl["axes"]:
                per_cyl["axes"].append(axis_value)
            if add_value and add_value not in per_cyl["adds"]:
                per_cyl["adds"].append(add_value)
        return {"mode": self.mode, "colors": colors, "tree": tree,
                "base_curves": curves, "lists": {}}


class Rules(object):
    """What is orderable, stated one parameter at a time.

    A selection is accepted when every parameter this lens type is configured on
    carries a stated value and no other parameter carries one at all. That is
    the whole of it: the source asserts the lists and asserts no dependency
    between them, so inventing one here would refuse orders the supplier accepts.
    """

    mode = "RULES"

    def __init__(self, lists, lens_type):
        self.lists = {p: list(v or ()) for p, v in (lists or {}).items() if v}
        self.lens_type = (lens_type or "SPHERICAL").strip().upper()

    def values(self, param):
        return [entry["value"] for entry in self.lists.get(param, ())]

    def configured_on(self):
        """The parameters this lens is chosen on: its type's, plus base curve.

        Base curve is per product rather than per type — a daily sphere may be
        made in two curves and another in one — so it is configured when values
        for it were stated.
        """
        params = list(TYPE_PARAMS.get(self.lens_type, ("sph",)))
        if self.values("base_curve") and "base_curve" not in params:
            params.insert(0, "base_curve")
        return tuple(params)

    def find(self, selection):
        configured = self.configured_on()
        for param in PARAMS:
            chosen = _selected(selection, param)
            if param in configured:
                # "0.00" is plano and is truthy as text, so a real power of zero
                # is a choice like any other; "" is no choice at all.
                if not chosen or chosen not in self.values(param):
                    return None
            elif chosen:
                return None
        found = {"variant_id": None}
        for param in PARAMS:
            found[param] = _selected(selection, param) or None
        found["color_code"] = found.pop("color")
        found["color_name"] = self._label("color", found["color_code"])
        found["diameter"] = (self.values("diameter") or [None])[0]
        return found

    def _label(self, param, value):
        for entry in self.lists.get(param, ()):
            if entry["value"] == value:
                return entry["label"]
        return None

    def options(self):
        colors = [{"code": e["value"], "name": e["label"]}
                  for e in self.lists.get("color", ())]
        configured = self.configured_on()
        return {"mode": self.mode, "colors": colors, "tree": {},
                "base_curves": self.values("base_curve"),
                "lists": {p: list(self.lists.get(p, ()))
                          for p in configured}}


def selectable(source, lens_type=None):
    """The stated shape for a lens, whichever it was given in.

    ``source`` is a list of variant rows, a dict of parameter lists, or an
    already-built ``Matrix``/``Rules``; callers that hold one of those do not
    have to know which the lens uses.
    """
    if isinstance(source, (Matrix, Rules)):
        return source
    if isinstance(source, dict):
        return Rules(source, lens_type)
    return Matrix(source)


def options(source, lens_type=None):
    """The choices a customer may make, for the template and the JSON it ships."""
    return selectable(source, lens_type).options()


def find(source, selection, lens_type=None):
    """The stated combination a selection names, or ``None``."""
    return selectable(source, lens_type).find(selection)


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
        "base_curve": (form.get("%s_bc" % eye) or "").strip(),
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


def _int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def minimums(product, site=None):
    """The minimum boxes this storefront enforces for this lens.

    Product-specific, because the supply terms are: 8 boxes for one eye of
    Acuvue Moist, 4 per eye when both are ordered. ``.in`` has no minimum by
    decision, so this returns zeros there and the check below does nothing.
    """
    if site and site != "optiwar.com":
        return {"single": 0, "both": 0}
    return {"single": _int((product or {}).get("min_boxes_single_eye")),
            "both": _int((product or {}).get("min_boxes_both_per_eye"))}


def _minimum_problems(product, lines, site):
    """Whether the boxes ordered clear this lens's minimum, per eye.

    ``both`` is per eye and not a total: four boxes for both eyes means four
    left and four right, eight in all.
    """
    limits = minimums(product, site)
    if len(lines) > 1:
        floor, wording = limits["both"], "%d boxes per eye when ordering both"
    else:
        floor, wording = limits["single"], "%d boxes for a single eye"
    if not floor:
        return []
    return [(REFUSED_MINIMUM,
             "%s eye: this lens is supplied in a minimum of %s"
             % (line["eye"], wording % floor))
            for line in lines if line["boxes"] < floor]


def _fill_single_choices(shape, selection):
    """Answer for the customer any parameter that has exactly one stated value.

    A lens made in one base curve is not a question, and asking it would make
    the difference between a product with two curves and a product with one a
    difference in how many dropdowns are on the page.
    """
    for param in PARAMS:
        if _selected(selection, param):
            continue
        values = shape.values(param)
        if len(values) == 1:
            selection[param if param != "color" else "color"] = values[0]
    return selection


def validate_detailed(source, product, selections, site=None, lens_type=None):
    """Accept a per-eye order, or say why not.

    Returns ``(lines, problems)``. ``lines`` carry the matched combination and
    the boxes for each eye that was ordered; a non-empty ``problems`` means
    nothing should be added to a cart. Each problem is ``(code, sentence)`` so
    the same refusal can be shown to the customer and counted by its reason.
    """
    shape = selectable(source, lens_type or (product or {}).get("lens_type"))
    problems, lines = [], []
    for sel in selections:
        if not sel.get("boxes"):
            continue
        if sel["boxes"] > MAX_BOXES_PER_EYE:
            problems.append((REFUSED_BOXES, "%s eye: at most %d boxes per eye"
                             % (sel["eye"], MAX_BOXES_PER_EYE)))
            continue
        variant = shape.find(_fill_single_choices(shape, dict(sel)))
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
    problems.extend(_minimum_problems(product, lines, site))
    if not box_price(product) and not problems:
        problems.append((REFUSED_NO_PRICE, "This lens has no price"))
    if problems:
        lines = []
    return lines, problems


def validate(source, product, selections, site=None, lens_type=None):
    """``validate_detailed`` for a caller that only shows the sentences."""
    lines, problems = validate_detailed(source, product, selections,
                                        site=site, lens_type=lens_type)
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
    curve = _num(variant.get("base_curve"))
    if curve:
        parts.append("BC %s" % curve)
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
            "%s_bc" % eye: _num(variant.get("base_curve")),
            "%s_lens_color" % eye: (variant.get("color_code") or ""),
            "%s_variant_id" % eye: variant.get("variant_id"),
            "%s_eye" % eye: (describe(variant, count) if count
                             else "No RX selected"),
        })
    item["order_quantity"] = total_boxes
    item["ATC_WCL"] = round(price * total_boxes, 2)
    item["total_savings"] = 0
    return item
