"""Tiered ordering rules for a contact lens, compiled into what is orderable.

A manufacturer's chart does not list combinations one at a time; it states
tiers — "spheres -6.00 to -0.25 in cylinders -0.75/-1.25/-1.75 at axes 10 to
180 step 10, from stock; spheres +0.25 to +4.00 in the same cylinders and axes,
made to order, 8-10 weeks; cylinder -2.25 only at axes 20, 70, 90, 110, 160,
180". That is the shape a person can read against the chart and confirm, and
the shape an AI extraction is asked to produce. It is NOT the shape checkout
reads: checkout reads ``contact_lens_variants``, one row per combination
(``lens_order.Matrix``), because a customer's selection is looked up and never
computed.

This module is the deterministic step between the two. A rule set (JSON, one
document per product, kept verbatim in ``contact_lens_rule_sets`` with who
confirmed it and against what) is compiled into

  * the complete list of orderable combinations, each carrying its fulfilment
    status and its natural-language lead time, and
  * one compiled configuration object (``contact_lens_configs``) the runtime
    serves from cache — the rows in a compact form, the fulfilment legend and
    the counts — keyed by ``(product_id, rule_version)``.

Compilation is a pure function of the rule set: the same document compiles to
the same rows in the same order with the same checksum, on any machine, or it
is refused. Nothing here reads a request, a session or a process global.

Publication is one pointer: ``contact_lens_products.rule_version``. The rows
and the config for version N are written and committed, and only then does the
product row start naming N. A reader holding N-1 keeps its own config (cache
keys carry the version) and the next reader gets N whole; there is no moment
at which half a matrix is visible.

Fulfilment vocabulary, per combination:

``STANDARD``       stocked; ships in the product's standard window
``MADE_TO_ORDER``  made by the manufacturer on demand; the tier states a lead
                   time the customer must see before ordering
``UNAVAILABLE``    stated so a hole in a range is a decision and not an
                   omission; compiles to no row

Overlap between tiers is an error unless the later tier says ``override``:
MyDay's axis-specific made-to-order values sit inside an otherwise standard
range, and saying so explicitly is what makes the compiled matrix a statement
of the chart rather than of the order two tiers happened to be typed in.
"""
import datetime
import hashlib
import json
import re
from decimal import Decimal, ROUND_HALF_UP

try:
    from . import lens_order
except ImportError:  # run as a plain module (tests, deploy tool, scripts)
    import lens_order

FULFILMENT_STANDARD = "STANDARD"
FULFILMENT_MADE_TO_ORDER = "MADE_TO_ORDER"
FULFILMENT_UNAVAILABLE = "UNAVAILABLE"
FULFILMENTS = (FULFILMENT_STANDARD, FULFILMENT_MADE_TO_ORDER,
               FULFILMENT_UNAVAILABLE)

RULE_STATUS_DRAFT = "DRAFT"
RULE_STATUS_COMPILED = "COMPILED"
RULE_STATUS_PUBLISHED = "PUBLISHED"

CONFIG_SCHEMA_VERSION = 1

# The rule set as confirmed, verbatim. Versions are per product and ascend;
# the row for a version is never rewritten, so what the customer was offered
# under version N can be read back after N+1 is live.
RULE_SETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_rule_sets (
    rule_set_id   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    product_id    INT NOT NULL,
    rule_version  INT NOT NULL,
    status        VARCHAR(12) NOT NULL DEFAULT 'DRAFT',
    rules_json    LONGTEXT NOT NULL,
    rules_sha256  CHAR(64) NOT NULL,
    source_type   VARCHAR(40) NULL,
    source_ref    VARCHAR(200) NULL,
    source_date   DATE NULL,
    confirmed_by  VARCHAR(80) NULL,
    confirmed_at  DATETIME NULL,
    compiled_at   DATETIME NULL,
    published_at  DATETIME NULL,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_cl_rule_set (product_id, rule_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# One compiled configuration per (product, version): immutable once written,
# read by the runtime through the cache, and the only thing the runtime needs
# to offer and validate a selection without touching the variant rows.
CONFIGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_lens_configs (
    product_id        INT NOT NULL,
    rule_version      INT NOT NULL,
    config_json       LONGTEXT NOT NULL,
    config_sha256     CHAR(64) NOT NULL,
    combinations      INT UNSIGNED NOT NULL,
    made_to_order     INT UNSIGNED NOT NULL DEFAULT 0,
    compiled_at       DATETIME NOT NULL,
    published_at      DATETIME NULL,
    PRIMARY KEY (product_id, rule_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (
    ("contact_lens_rule_sets", RULE_SETS_SCHEMA),
    ("contact_lens_configs", CONFIGS_SCHEMA),
)

# Columns the compiler writes on every combination row. Additive: rows loaded
# before these existed read back as STANDARD with no lead time, which is what
# they were.
VARIANT_COLUMNS = (
    ("fulfilment_status", "VARCHAR(16) NOT NULL DEFAULT 'STANDARD'"),
    ("lead_time_text", "VARCHAR(80) NULL"),
    ("lead_time_min_days", "SMALLINT UNSIGNED NULL"),
    ("lead_time_max_days", "SMALLINT UNSIGNED NULL"),
    ("lead_time_source", "VARCHAR(200) NULL"),
)


class RuleError(ValueError):
    """The rule set cannot be compiled, and this is why. Never a guess."""


# ---------------------------------------------------------------------------
# Lead time: the customer reads the words; the range is for sorting, feeds and
# the order snapshot. Words nobody can parse keep their words and no range.
# ---------------------------------------------------------------------------

_UNIT_DAYS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30,
              "months": 30}
_RANGE = re.compile(r"(\d+)\s*(?:-|–|to)\s*(\d+)\s*(days?|weeks?|months?)",
                    re.IGNORECASE)
_UP_TO = re.compile(r"(?:up\s*to|within|max(?:imum)?)\s*(\d+)\s*"
                    r"(days?|weeks?|months?)", re.IGNORECASE)
_SINGLE = re.compile(r"(\d+)\s*(days?|weeks?|months?)", re.IGNORECASE)


def lead_time_days(text):
    """``(min_days, max_days)`` read out of natural wording, or ``(None, None)``.

    "8-10 weeks" -> (56, 70); "Up to 45 days" -> (None, 45);
    "Ships within 3-5 days" -> (3, 5); "6 weeks" -> (42, 42).
    """
    text = (text or "").strip()
    if not text:
        return None, None
    m = _RANGE.search(text)
    if m:
        unit = _UNIT_DAYS[m.group(3).lower()]
        lo, hi = int(m.group(1)) * unit, int(m.group(2)) * unit
        if lo > hi:
            raise RuleError("lead time %r runs backwards" % text)
        return lo, hi
    m = _UP_TO.search(text)
    if m:
        return None, int(m.group(1)) * _UNIT_DAYS[m.group(2).lower()]
    m = _SINGLE.search(text)
    if m:
        days = int(m.group(1)) * _UNIT_DAYS[m.group(2).lower()]
        return days, days
    return None, None


# ---------------------------------------------------------------------------
# Values: a parameter is stated as an explicit list, or as a closed range with
# a step. Both become canonical strings (lens_order's), and a range that does
# not land on its own end is refused rather than rounded.
# ---------------------------------------------------------------------------

_QUARTER = Decimal("0.25")
_STEP_MIN = Decimal("0.01")


def _dec(value, what):
    try:
        return Decimal(str(value).strip())
    except Exception:  # noqa: BLE001 - message is the point
        raise RuleError("%s: %r is not a number" % (what, value))


def _expand(spec, what, integer=False):
    """The stated values of one parameter, canonical and in ascending order."""
    if spec is None:
        return []
    if isinstance(spec, dict):
        for key in ("from", "to", "step"):
            if key not in spec:
                raise RuleError("%s: range needs from/to/step" % what)
        lo, hi, step = (_dec(spec["from"], what), _dec(spec["to"], what),
                        _dec(spec["step"], what))
        if step <= 0:
            raise RuleError("%s: step must be positive" % what)
        if lo > hi:
            raise RuleError("%s: range %s..%s runs backwards" % (what, lo, hi))
        if (hi - lo) % step != 0:
            raise RuleError("%s: %s..%s does not divide by step %s"
                            % (what, lo, hi, step))
        values, cur = [], lo
        while cur <= hi:
            values.append(cur)
            cur += step
        if spec.get("exclude"):
            drop = {_dec(v, what) for v in spec["exclude"]}
            values = [v for v in values if v not in drop]
    elif isinstance(spec, (list, tuple)):
        values = [_dec(v, what) for v in spec]
    else:
        values = [_dec(spec, what)]
    out = []
    for value in sorted(set(values)):
        if integer:
            if value != value.to_integral_value():
                raise RuleError("%s: %s is not a whole number" % (what, value))
            out.append(str(int(value)))
        else:
            if value % _STEP_MIN != 0:
                raise RuleError("%s: %s has more than two decimals"
                                % (what, value))
            out.append("%.2f" % value.quantize(Decimal("0.01"), ROUND_HALF_UP))
    return out


def _check_sph(values):
    for v in values:
        d = Decimal(v)
        if d < -30 or d > 30 or d % _QUARTER != 0:
            raise RuleError("sph %s is not a quarter-dioptre power within "
                            "±30.00" % v)


def _check_cyl(values):
    for v in values:
        d = Decimal(v)
        if d == 0 or abs(d) > 10 or d % _QUARTER != 0:
            raise RuleError("cyl %s is not a non-zero quarter-dioptre "
                            "cylinder within ±10.00" % v)


def _check_axis(values):
    for v in values:
        if not 1 <= int(v) <= 180:
            raise RuleError("axis %s is outside 1..180" % v)


def _check_add(values):
    for v in values:
        d = Decimal(v)
        if d <= 0 or d > 4 or d % _QUARTER != 0:
            raise RuleError("add %s is not a positive quarter-dioptre "
                            "addition up to +4.00" % v)


_CHECKS = {"sph": _check_sph, "cyl": _check_cyl, "axis": _check_axis,
           "add_power": _check_add}
_INTEGER = {"axis"}

# The parameters a tier must state for each lens type, and may not state
# otherwise: a spherical tier with a cylinder is a chart misread.
_TIER_PARAMS = {t: tuple(p for p in params if p != "color")
                for t, params in lens_order.TYPE_PARAMS.items()}
_COLOR_TYPES = {"COLOR"}

# A combination's identity inside the compiler and the compiled config.
SIG_FIELDS = ("color_code", "base_curve", "sph", "cyl", "axis", "add_power")


def signature(row):
    """The key two combinations are the same combination on."""
    return "|".join(lens_order._canonical(f, row.get(f)) or ""
                    for f in SIG_FIELDS)


# ---------------------------------------------------------------------------
# Compilation.
# ---------------------------------------------------------------------------

def _colors(rules, lens_type):
    colors = rules.get("colors") or []
    if lens_type in _COLOR_TYPES and not colors:
        raise RuleError("a COLOR lens states its colours")
    if colors and lens_type not in _COLOR_TYPES:
        raise RuleError("colours stated for a %s lens" % lens_type)
    out = []
    for entry in colors:
        if not isinstance(entry, dict) or not entry.get("code"):
            raise RuleError("colour needs a code")
        out.append({"code": str(entry["code"]).strip(),
                    "name": str(entry.get("name") or entry["code"]).strip()})
    if len({c["code"] for c in out}) != len(out):
        raise RuleError("duplicate colour code")
    return out


def _fulfilment(tier, index):
    status = str(tier.get("fulfilment") or FULFILMENT_STANDARD).strip().upper()
    if status not in FULFILMENTS:
        raise RuleError("tier %d: fulfilment %r is not one of %s"
                        % (index, status, ", ".join(FULFILMENTS)))
    text = (tier.get("lead_time") or "").strip() or None
    if status == FULFILMENT_MADE_TO_ORDER and not text:
        raise RuleError("tier %d: made to order without a lead time the "
                        "customer can read" % index)
    if status != FULFILMENT_MADE_TO_ORDER and text:
        raise RuleError("tier %d: a lead time belongs to a made-to-order tier;"
                        " the standard window is the product's" % index)
    lo, hi = lead_time_days(text)
    source = (tier.get("source") or "").strip() or None
    if status == FULFILMENT_MADE_TO_ORDER and not source:
        raise RuleError("tier %d: made to order without naming the chart it "
                        "comes from" % index)
    return {"status": status, "lead_time_text": text,
            "lead_time_min_days": lo, "lead_time_max_days": hi,
            "source": source}


def _tier_values(tier, params, index):
    values = {}
    for param in params:
        raw = tier.get(param)
        if raw is None:
            raise RuleError("tier %d: %s not stated for this lens type"
                            % (index, param))
        vals = _expand(raw, "tier %d %s" % (index, param),
                       integer=param in _INTEGER)
        if not vals:
            raise RuleError("tier %d: %s states no values" % (index, param))
        _CHECKS[param](vals)
        values[param] = vals
    for param in ("sph", "cyl", "axis", "add_power"):
        if param not in params and tier.get(param) is not None:
            raise RuleError("tier %d: %s stated but this lens type has none"
                            % (index, param))
    return values


def compile_rules(rules, product_id=None, rule_version=None):
    """Compile one rule set into rows and a config. Pure, deterministic.

    Returns ``{"rows": [...], "config": {...}, "counts": {...}}``. Each row
    is a variant dict in the shape ``contact_lens_variants`` stores, with
    ``fulfilment_status``, ``lead_time_*`` and no ``variant_id``. Raises
    ``RuleError`` for anything the chart did not say clearly.
    """
    if not isinstance(rules, dict):
        raise RuleError("rule set is not an object")
    lens_type = str(rules.get("lens_type") or "").strip().upper()
    if lens_type not in _TIER_PARAMS:
        raise RuleError("lens_type %r is not one of %s"
                        % (lens_type, ", ".join(sorted(_TIER_PARAMS))))
    params = _TIER_PARAMS[lens_type]
    curves = _expand(rules.get("base_curve"), "base_curve")
    if not curves:
        raise RuleError("base_curve not stated")
    diameters = _expand(rules.get("diameter"), "diameter")
    if len(diameters) != 1:
        raise RuleError("exactly one diameter must be stated")
    diameter = diameters[0]
    colors = _colors(rules, lens_type) or [{"code": "", "name": None}]
    tiers = rules.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        raise RuleError("no tiers stated")

    legend = [{"status": FULFILMENT_STANDARD, "lead_time_text": None,
               "lead_time_min_days": None, "lead_time_max_days": None,
               "source": None}]
    rows = {}
    owner = {}
    for index, tier in enumerate(tiers, 1):
        if not isinstance(tier, dict):
            raise RuleError("tier %d is not an object" % index)
        fulfil = _fulfilment(tier, index)
        values = _tier_values(tier, params, index)
        tier_curves = _expand(tier.get("base_curve"), "tier %d base_curve"
                              % index) or curves
        for c in tier_curves:
            if c not in curves:
                raise RuleError("tier %d: base curve %s is not one the lens "
                                "is made in" % (index, c))
        override = bool(tier.get("override"))
        legend_index = _legend_index(legend, fulfil)
        for combo in _cartesian(colors, tier_curves, values, params):
            combo["diameter"] = diameter
            sig = signature(combo)
            if sig in owner and not override:
                raise RuleError("tier %d restates %s already stated by tier "
                                "%d; say override if the later tier is the "
                                "chart's exception" % (index, sig, owner[sig]))
            owner[sig] = index
            if fulfil["status"] == FULFILMENT_UNAVAILABLE:
                rows.pop(sig, None)
                continue
            combo["fulfilment_status"] = fulfil["status"]
            combo["lead_time_text"] = fulfil["lead_time_text"]
            combo["lead_time_min_days"] = fulfil["lead_time_min_days"]
            combo["lead_time_max_days"] = fulfil["lead_time_max_days"]
            combo["lead_time_source"] = fulfil["source"]
            combo["_legend"] = legend_index
            rows[sig] = combo
    if not rows:
        raise RuleError("the tiers leave nothing orderable")
    ordered = [rows[s] for s in sorted(rows, key=_sort_key)]
    made = sum(1 for r in ordered
               if r["fulfilment_status"] == FULFILMENT_MADE_TO_ORDER)
    counts = {"combinations": len(ordered), "made_to_order": made,
              "standard": len(ordered) - made}
    config = {
        "schema": CONFIG_SCHEMA_VERSION,
        "product_id": product_id,
        "rule_version": rule_version,
        "lens_type": lens_type,
        "base_curves": curves,
        "diameter": diameter,
        "colors": [c for c in colors if c["code"]],
        "legend": legend,
        # [variant_id, color_code, base_curve, sph, cyl, axis, add, legend]
        "rows": [[None, r["color_code"], r["base_curve"], r["sph"],
                  r["cyl"], r["axis"], r["add_power"], r["_legend"]]
                 for r in ordered],
        "counts": counts,
    }
    config["checksum"] = checksum(config)
    for r in ordered:
        r.pop("_legend")
    return {"rows": ordered, "config": config, "counts": counts}


def _legend_index(legend, fulfil):
    for i, entry in enumerate(legend):
        if entry == fulfil:
            return i
    if fulfil["status"] == FULFILMENT_STANDARD:
        return 0
    legend.append(fulfil)
    return len(legend) - 1


def _cartesian(colors, curves, values, params):
    lists = [values.get(p) or [None] for p in ("sph", "cyl", "axis",
                                                 "add_power")]
    for color in colors:
        for curve in curves:
            for sph in lists[0]:
                for cyl in lists[1]:
                    for axis in lists[2]:
                        for add in lists[3]:
                            yield {"color_code": color["code"],
                                   "color_name": color["name"],
                                   "base_curve": curve, "sph": sph,
                                   "cyl": cyl, "axis": axis,
                                   "add_power": add}


def _sort_key(sig):
    parts = sig.split("|")

    def num(s):
        return Decimal(s) if s else Decimal("-999")
    return (parts[0], num(parts[1]), num(parts[2]), num(parts[3]),
            num(parts[4]), num(parts[5]))


def checksum(config):
    """sha256 of the config without its own checksum, canonically serialised."""
    body = {k: v for k, v in config.items() if k != "checksum"}
    return hashlib.sha256(json.dumps(body, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def rules_sha256(rules):
    return hashlib.sha256(json.dumps(rules, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Reading a compiled config back into the runtime's shape.
# ---------------------------------------------------------------------------

def rows_from_config(config):
    """Variant dicts (``lens_order.Matrix`` input) out of a compiled config."""
    legend = config.get("legend") or []
    colors = {c["code"]: c["name"] for c in (config.get("colors") or [])}
    out = []
    for entry in config.get("rows") or ():
        vid, color, bc, sph, cyl, axis, add, li = entry
        f = legend[li] if li < len(legend) else legend[0]
        out.append({
            "variant_id": vid, "color_code": color or "",
            "color_name": colors.get(color) if color else None,
            "base_curve": bc, "diameter": config.get("diameter"),
            "sph": sph, "cyl": cyl, "axis": axis, "add_power": add,
            "fulfilment_status": f["status"],
            "lead_time_text": f["lead_time_text"],
            "lead_time_min_days": f["lead_time_min_days"],
            "lead_time_max_days": f["lead_time_max_days"],
            "lead_time_source": f["source"],
        })
    return out


# ---------------------------------------------------------------------------
# Writing: rule set -> variants -> config -> pointer. The caller owns the
# transaction; nothing here commits, so a failure anywhere leaves the product
# exactly as it was, still naming the version it named before.
# ---------------------------------------------------------------------------

_VARIANT_COLS = ("product_id", "sph", "cyl", "axis", "add_power",
                 "base_curve", "diameter", "color_code", "color_name",
                 "available", "fulfilment_status", "lead_time_text",
                 "lead_time_min_days", "lead_time_max_days",
                 "lead_time_source")

_UPSERT = (
    "INSERT INTO contact_lens_variants (%s) VALUES (%s) "
    "ON DUPLICATE KEY UPDATE available = 1, color_name = VALUES(color_name), "
    "fulfilment_status = VALUES(fulfilment_status), "
    "lead_time_text = VALUES(lead_time_text), "
    "lead_time_min_days = VALUES(lead_time_min_days), "
    "lead_time_max_days = VALUES(lead_time_max_days), "
    "lead_time_source = VALUES(lead_time_source)"
    % (", ".join(_VARIANT_COLS), ", ".join(["%s"] * len(_VARIANT_COLS))))


def next_version(cursor, product_id):
    cursor.execute("SELECT COALESCE(MAX(rule_version), 0) AS v FROM "
                   "contact_lens_rule_sets WHERE product_id = %s",
                   (product_id,))
    row = cursor.fetchone()
    return int((row["v"] if isinstance(row, dict) else row[0]) or 0) + 1


def store_rule_set(cursor, product_id, rules, source_type=None,
                   source_ref=None, source_date=None, confirmed_by=None,
                   now=None):
    """Keep a confirmed rule set verbatim as the next version. Returns it.

    Compiles first, so a rule set that cannot compile is never stored as a
    version — but writes nothing else: compiling to rows and publishing are
    separate, later, deliberate steps.
    """
    compiled = compile_rules(rules)
    version = next_version(cursor, product_id)
    cursor.execute(
        "INSERT INTO contact_lens_rule_sets (product_id, rule_version, status,"
        " rules_json, rules_sha256, source_type, source_ref, source_date,"
        " confirmed_by, confirmed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (product_id, version, RULE_STATUS_DRAFT,
         json.dumps(rules, sort_keys=True), rules_sha256(rules),
         source_type, source_ref, source_date, confirmed_by,
         _stamp(now) if confirmed_by else None))
    return {"rule_version": version, "counts": compiled["counts"]}


def load_rule_set(cursor, product_id, rule_version):
    cursor.execute("SELECT * FROM contact_lens_rule_sets WHERE product_id=%s "
                   "AND rule_version=%s", (product_id, rule_version))
    row = cursor.fetchone()
    if not row:
        return None
    row = dict(row)
    row["rules"] = json.loads(row["rules_json"])
    return row


def compile_and_publish(cursor, product_id, rule_version, now=None,
                        publish=True):
    """Compile version N to rows and config; optionally make N the live one.

    Rows: every combination the compiled set states is upserted available;
    every other row of this product is marked unavailable, never deleted
    (an order line may still name its ``variant_id``). Config: written for
    (product, N). Pointer: ``contact_lens_products.rule_version = N`` in the
    same transaction — the caller's commit is the publication.
    """
    stored = load_rule_set(cursor, product_id, rule_version)
    if stored is None:
        raise RuleError("no rule set version %s for product %s"
                        % (rule_version, product_id))
    compiled = compile_rules(stored["rules"], product_id, rule_version)
    cursor.execute("UPDATE contact_lens_variants SET available = 0 "
                   "WHERE product_id = %s", (product_id,))
    for row in compiled["rows"]:
        cursor.execute(_UPSERT, tuple(
            [product_id] + [_db_value(row, c) for c in _VARIANT_COLS[1:]]))
    ids = _variant_ids(cursor, product_id)
    for entry, row in zip(compiled["config"]["rows"], compiled["rows"]):
        entry[0] = ids.get(signature(row))
        if entry[0] is None:
            raise RuleError("compiled row %s did not reach the database"
                            % signature(row))
    config = compiled["config"]
    config["checksum"] = checksum(config)
    stamp = _stamp(now)
    cursor.execute(
        "INSERT INTO contact_lens_configs (product_id, rule_version, "
        "config_json, config_sha256, combinations, made_to_order, "
        "compiled_at, published_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE config_json = VALUES(config_json), "
        "config_sha256 = VALUES(config_sha256), "
        "combinations = VALUES(combinations), "
        "made_to_order = VALUES(made_to_order), "
        "compiled_at = VALUES(compiled_at), "
        "published_at = VALUES(published_at)",
        (product_id, rule_version, json.dumps(config, separators=(",", ":")),
         config["checksum"], config["counts"]["combinations"],
         config["counts"]["made_to_order"], stamp,
         stamp if publish else None))
    cursor.execute(
        "UPDATE contact_lens_rule_sets SET status=%s, compiled_at=%s, "
        "published_at=%s WHERE product_id=%s AND rule_version=%s",
        (RULE_STATUS_PUBLISHED if publish else RULE_STATUS_COMPILED, stamp,
         stamp if publish else None, product_id, rule_version))
    if publish:
        cursor.execute(
            "UPDATE contact_lens_products SET rule_version = %s, "
            "param_mode = 'MATRIX', matrix_version = matrix_version + 1 "
            "WHERE product_id = %s", (rule_version, product_id))
    return {"rule_version": rule_version, "counts": config["counts"],
            "checksum": config["checksum"], "published": publish}


def _variant_ids(cursor, product_id):
    cursor.execute("SELECT variant_id, sph, cyl, axis, add_power, base_curve,"
                   " color_code FROM contact_lens_variants WHERE product_id=%s"
                   " AND available = 1", (product_id,))
    out = {}
    for row in cursor.fetchall() or ():
        row = dict(row)
        out[signature(row)] = int(row["variant_id"])
    return out


def _db_value(row, column):
    value = row.get(column)
    if column == "available":
        return 1
    if column == "color_code":
        return value or ""
    if value == "":
        return None
    return value


def _stamp(now):
    return (now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
