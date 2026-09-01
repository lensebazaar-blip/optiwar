#!/usr/bin/env python3
"""Turn a LensBazaar export workbook into the importer's two sheets.

The export is an OpenCart dump: one product sheet, and a "variants" sheet that
is one row per *selectable dropdown value* per eye — not per sellable
combination. Their notes say so explicitly, and say they hold no manufacturer
combination availability at all. So this writes the RULES shape:

    products.csv   one row per commercial box offer
    rules.csv      one row per selectable value of one parameter

and never a combinations sheet, because multiplying their lists together would
assert a chart (53 x 4 x 18 = 3,816 rows for MyDay Toric) that no manufacturer
published.

    python3 scripts/normalize_lensbazaar_export.py \
        --export contact_lenses_master.xlsx --out /tmp/pilot

What it deliberately does not carry over:

``manufacturer_mpn``  their column holds the product's own name, which is not a
                      manufacturer part number. Left empty; the feed then sends
                      identifier_exists=false.
``gtin``              not held for lenses (their note 5). Left empty.
``image_url``         not supplied (their note 6). Left empty, so the importer
                      refuses the product until Optiwar's own photograph exists.
``stock_units``       a lens counter that goes negative on backorder (note 8),
                      so availability comes from ``enabled``, not from it.
``BOXES``             their per-eye quantity dropdown is a UI range, not a
                      prescription parameter; minimum boxes and our 24-box cap
                      decide it.

Both eyes' lists are checked to be identical and collapsed into one; a lens
whose LEFT and RIGHT differ is reported rather than silently halved.
"""
import argparse
import collections
import csv
import os
import sys

# Their parameter names, and ours. BOXES is not a prescription parameter.
PARAMETERS = {"SPH": "sph", "CYL": "cyl", "AXIS": "axis", "ADD": "add_power",
              "BASE CURVE": "base_curve", "COLOUR": "color", "COLOR": "color"}
SKIP_PARAMETERS = ("BOXES", "QUANTITY")

LENS_TYPES = ((("toric", "multifocal"), "TORIC_MULTIFOCAL"),
              (("toric", "progressive"), "TORIC_MULTIFOCAL"),
              (("toric",), "TORIC"),
              (("multifocal",), "MULTIFOCAL"),
              (("progressive",), "MULTIFOCAL"),
              (("colour",), "COLOR"),
              (("color",), "COLOR"))


def read_sheet(book, name):
    sheet = book[name]
    rows = sheet.iter_rows(values_only=True)
    header = [str(c or "").strip() for c in next(rows)]
    return [dict(zip(header, row)) for row in rows]


def text(value):
    return "" if value is None else str(value).strip()


def number(value):
    """The leading number of "8.6 MM" / "51% H2O", or "" if there isn't one."""
    kept = []
    for char in text(value):
        if char.isdigit() or (char == "." and kept):
            kept.append(char)
        elif kept:
            break
    return "".join(kept)


def lens_type(raw):
    lowered = text(raw).lower()
    for words, name in LENS_TYPES:
        # Every word: "toric" alone is a toric, and only "toric multifocal" is
        # both.
        if all(word in lowered for word in words):
            return name
    return "SPHERICAL"


def modality(raw):
    lowered = text(raw).lower()
    for word, name in (("daily", "DAILY"), ("monthly", "MONTHLY"),
                       ("fortnight", "BIWEEKLY"), ("two week", "BIWEEKLY"),
                       ("bi-week", "BIWEEKLY"), ("yearly", "YEARLY")):
        if word in lowered:
            return name
    return ""


def product_row(row):
    return {
        "source_ref": text(row.get("lb_product_id")),
        "manufacturer": text(row.get("manufacturer")),
        "brand": text(row.get("brand")),
        "product_name": text(row.get("product_name")),
        "gtin": "",
        "manufacturer_mpn": "",
        "image_url": "",
        "modality": modality(row.get("lens_type_modality_raw")),
        "lens_type": lens_type(row.get("lens_type_modality_raw")),
        "pack_quantity": number(row.get("pack_size_raw")),
        "material": text(row.get("material")),
        "water_content": number(row.get("water_content")),
        "replacement_days": "1" if modality(
            row.get("lens_type_modality_raw")) == "DAILY" else "",
        # enabled, not stock_units: their counter is lenses and goes negative on
        # backorder, and a lens is never out of stock the way a frame is.
        "availability": ("IN_STOCK" if text(row.get("enabled")) == "1"
                         else "ON_ORDER"),
        "price_eur": text(row.get("eur_price_regular_per_box")),
        "special_price_eur": text(row.get("eur_price_sale_per_box")),
        "min_boxes_single_eye": text(row.get("min_boxes_single_eye")),
        "min_boxes_both_per_eye": text(row.get("min_boxes_both_eyes_per_eye")),
        "source_url": text(row.get("lensbazaar_url")),
        "description": text(row.get("description_text")),
        "param_mode": "RULES",
        "param_source": "",             # supplied by --param-source
        "base_curve": text(row.get("base_curve")),
        "diameter": text(row.get("diameter")),
    }


def rule_rows(variant_rows, products):
    """(rows, problems). One row per parameter value, both eyes agreeing."""
    per_eye = collections.defaultdict(lambda: collections.defaultdict(list))
    for row in variant_rows:
        parameter = text(row.get("parameter")).upper()
        if parameter in SKIP_PARAMETERS:
            continue
        if parameter not in PARAMETERS:
            continue
        ref = text(row.get("lb_product_id"))
        eye = text(row.get("eye")).upper()
        value = text(row.get("value")).rstrip("\u00b0")   # "10°" -> "10"
        available = "0" if text(row.get("available")) == "0" else "1"
        per_eye[(ref, PARAMETERS[parameter])][eye].append((value, available))

    rows, problems = [], []
    for (ref, parameter), eyes in sorted(per_eye.items()):
        sides = {eye: [v for v, a in values if a == "1"]
                 for eye, values in eyes.items()}
        left, right = sides.get("LEFT"), sides.get("RIGHT")
        if left is not None and right is not None and set(left) != set(right):
            problems.append("%s %s: LEFT and RIGHT offer different values"
                            % (ref, parameter))
            continue
        values = left if left is not None else next(iter(sides.values()))
        # An OpenCart option list repeats a value once per option row; the same
        # curve offered twice is one selectable curve.
        values = list(dict.fromkeys(values))
        for order, value in enumerate(values):
            rows.append({"source_ref": ref, "parameter": parameter,
                         "value": value, "sort_order": order, "available": 1})

    # BC and DIA are product facts on their product sheet, and a selectable
    # value only when more than one is offered. Both are stated either way, so a
    # prescription records the curve the lens was ordered in.
    for product in products:
        ref = product["source_ref"]
        for parameter, raw in (("base_curve", product.pop("base_curve")),
                              ("diameter", product.pop("diameter"))):
            if any(r["source_ref"] == ref and r["parameter"] == parameter
                   for r in rows):
                continue
            for order, part in enumerate(raw.replace("/", ",").split(",")):
                value = number(part)
                if value:
                    rows.append({"source_ref": ref, "parameter": parameter,
                                 "value": value, "sort_order": order,
                                 "available": 1})
    return rows, problems


def write(path, rows):
    if not rows:
        sys.exit("nothing to write to %s" % path)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print("  %-24s %d row(s)" % (os.path.basename(path), len(rows)))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", required=True, help="the supplier's workbook")
    ap.add_argument("--out", required=True, help="directory for the two CSVs")
    ap.add_argument("--param-source", default=None,
                    help="whose ordering rule these values are; required, "
                         "because a rules product records who asserted them")
    args = ap.parse_args()

    try:
        import openpyxl
    except ImportError:
        sys.exit("openpyxl is not installed: pip install openpyxl")
    book = openpyxl.load_workbook(args.export, read_only=True, data_only=True)

    products = [product_row(r) for r in read_sheet(book, "products")
                if text(r.get("lb_product_id"))]
    if not args.param_source:
        sys.exit("--param-source is required: the stated values are a "
                 "supplier's ordering rule and have to say whose")
    for product in products:
        product["param_source"] = args.param_source

    rules, problems = rule_rows(read_sheet(book, "variants"), products)
    os.makedirs(args.out, exist_ok=True)
    write(os.path.join(args.out, "products.csv"), products)
    write(os.path.join(args.out, "rules.csv"), rules)

    counts = collections.Counter(r["source_ref"] for r in rules)
    for product in products:
        print("  %-6s %-28s %-18s %s value(s)"
              % (product["source_ref"], product["product_name"][:28],
                 product["lens_type"], counts[product["source_ref"]]))
    for problem in problems:
        print("  PROBLEM %s" % problem)
    print("\nImages and identifiers are deliberately empty: the export supplies "
          "neither, and the importer refuses a lens with no image.")


if __name__ == "__main__":
    main()
