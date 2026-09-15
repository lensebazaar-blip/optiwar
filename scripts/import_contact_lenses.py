#!/usr/bin/env python3
"""Import a contact-lens export into products + profile + matrix.

Dry run by default. Nothing is written until a person types ``--apply``, and
what is written is decided entirely by ``cl_import.parse()``, so a rejection is
provable in a test without a database.

    # what would happen
    python3 scripts/import_contact_lenses.py \
        --products products.csv --variants variants.csv

    # do it
    python3 scripts/import_contact_lenses.py \
        --products products.csv --variants variants.csv --by sudhanshu --apply

CSV (or TSV) is what this reads. An .xlsx is read only if openpyxl happens to be
installed — the export arrives as a workbook, and one sheet saved as CSV is a
smaller dependency than a spreadsheet parser in the deployment.

Per product, in one transaction:

    products                    the commercial record, CONTACT_LENS, .com only
    contact_lens_products       brand, manufacturer, modality, pack, minimums
    contact_lens_param_rules    the stated values, when param_mode is RULES
    contact_lens_variants       the matrix, when param_mode is MATRIX
    contact_lens_images         the primary image, or every approved view when
                                ``--images`` names the product's image recipe

A product is stated in one shape or the other, never both: ``--rules`` carries
the parameter sheet and ``--variants`` the combinations sheet, and the product
row's ``param_mode`` says which of them applies to it.

Idempotent on (source_system, source_ref): re-running the same export updates
the same products instead of making new ones. Nothing is ever deleted — a
combination the manufacturer withdraws becomes ``available = 0``, so an order
that referenced it stays explicable.

``merchant_enabled`` stays 0. An imported lens is in the database and on no
surface until somebody releases it.

Connection comes from MYSQL_HOST/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DB, the same
variables gunicorn runs with; on the box they are the service's Environment=
lines, so export them from ``systemctl show gunicorn -p Environment`` before
running it there.
"""
import argparse
import csv
import datetime
import os
import sys

import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cl_import  # noqa: E402
import contact_lens  # noqa: E402
import image_pipeline  # noqa: E402
import lens_import_write  # noqa: E402

SELL_ON = lens_import_write.SELL_ON


def read_rows(path):
    if path.lower().endswith((".xlsx", ".xlsm")):
        return read_workbook(path)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delimiter = "\t" if "\t" in sample.splitlines()[0] else ","
        return [dict(row) for row in csv.DictReader(fh, delimiter=delimiter)]


def read_workbook(path):
    try:
        import openpyxl
    except ImportError:
        sys.exit("%s is a workbook and openpyxl is not installed — save the "
                 "sheet as CSV, or pip install openpyxl" % path)
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = book[book.sheetnames[0]]
    rows = sheet.iter_rows(values_only=True)
    header = [str(c or "").strip() for c in next(rows)]
    return [dict(zip(header, row)) for row in rows]


SOCKETS = ("/var/lib/mysql/mysql.sock", "/var/run/mysqld/mysqld.sock")


def connect():
    """The connection gunicorn has: MySQLdb reads ``localhost`` as the unix
    socket, and a box whose ``localhost`` resolves to ::1 while the server
    listens on IPv4 refuses the TCP form, so the socket is used when it is
    there."""
    host = os.environ.get("MYSQL_HOST", "localhost")
    options = {}
    socket_path = os.environ.get("MYSQL_UNIX_SOCKET") or next(
        (p for p in SOCKETS if host == "localhost" and os.path.exists(p)),
        None)
    if socket_path:
        options["unix_socket"] = socket_path
    return pymysql.connect(
        host=host,
        user=os.environ.get("MYSQL_USER", ""),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DB", ""),
        cursorclass=pymysql.cursors.DictCursor, autocommit=False, **options)


# The writers live in lens_import_write so the Ops console and this CLI write
# a lens identically; the names are re-exported for the tests that import them.
existing = lens_import_write.existing
product_code = lens_import_write.product_code
slug = lens_import_write.slug
in_rupees = lens_import_write.in_rupees
upsert_product = lens_import_write.upsert_product
upsert_profile = lens_import_write.upsert_profile
upsert_variants = lens_import_write.upsert_variants
upsert_rules = lens_import_write.upsert_rules
upsert_image = lens_import_write.upsert_image
withdraw_all = lens_import_write.withdraw_all


def upsert_views(cursor, recipe, product_id):
    return lens_import_write.upsert_views(
        cursor, image_pipeline.image_records(recipe), product_id)


def recipe_for(recipes, product):
    """The image recipe whose product code is this source_ref, or None."""
    return recipes.get(product["source_ref"].upper())


def image_url_is_primary(recipe, image_url):
    """True only for the recipe's primary view: the product's lead image is
    the hero, never a gallery view that happens to be in the recipe."""
    return any(r["is_primary"] and r["path"] == image_url
               for r in image_pipeline.image_records(recipe))


def import_one(cursor, product, rate=None, recipe=None):
    """One product and everything it states, inside the caller's transaction."""
    return lens_import_write.import_one(
        cursor, product, rate,
        image_pipeline.image_records(recipe) if recipe else None)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--products", required=True, help="product sheet (CSV/TSV)")
    ap.add_argument("--variants", default=None,
                    help="prescription combinations, for a MATRIX product")
    ap.add_argument("--rules", default=None,
                    help="selectable parameter values, for a RULES product")
    ap.add_argument("--only", default=None,
                    help="comma-separated source_refs — the pilot import")
    ap.add_argument("--by", default=None, help="who authorised it")
    ap.add_argument("--eur-inr-rate", type=float, default=None,
                    help="derive the INR columns from the EUR price at this "
                         "rate, and record the rate against the product")
    ap.add_argument("--images", action="append", default=[],
                    help="an image recipe (image_recipes/<CODE>.json); its "
                         "approved views become the product's image rows. "
                         "Repeatable. Matched to the product whose source_ref "
                         "is the recipe's product code")
    ap.add_argument("--apply", action="store_true",
                    help="write; otherwise dry run")
    args = ap.parse_args()

    recipes = {}
    for path in args.images:
        recipe = image_pipeline.load_recipe(path)
        recipes[recipe["product"].upper()] = recipe

    if not (args.variants or args.rules):
        sys.exit("--variants or --rules is required: a product row on its own "
                 "does not say what may be ordered")
    products, errors = cl_import.parse(
        read_rows(args.products),
        read_rows(args.variants) if args.variants else [],
        read_rows(args.rules) if args.rules else [])
    if args.only:
        wanted = {r.strip() for r in args.only.split(",") if r.strip()}
        unknown = wanted - {p["source_ref"] for p in products}
        if unknown:
            sys.exit("not importable (rejected or absent): %s"
                     % ", ".join(sorted(unknown)))
        products = [p for p in products if p["source_ref"] in wanted]

    print(cl_import.report(products, errors))
    for product in products:
        recipe = recipe_for(recipes, product)
        if recipe:
            records = image_pipeline.image_records(recipe)
            if not image_url_is_primary(recipe, product["image_url"]):
                sys.exit("%s: image_url %s is not the primary view of recipe %s"
                         % (product["source_ref"], product["image_url"],
                            recipe["product"]))
            print("  images %-14s %d view(s) from recipe, %d GMC-eligible"
                  % (product["source_ref"], len(records),
                     sum(1 for r in records if r["gmc"])))
            for level, text in image_pipeline.qa_warnings(recipe, records):
                print("         %-5s %s" % (level, text))
    unmatched = set(recipes) - {p["source_ref"].upper() for p in products}
    if unmatched:
        sys.exit("--images recipe(s) match no importable product: %s"
                 % ", ".join(sorted(unmatched)))
    if not products:
        sys.exit(1 if errors else 0)
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --by and --apply.")
        return
    if not args.by:
        sys.exit("--by is required to write")

    db = connect()
    cursor = db.cursor()
    contact_lens.ensure_schema(cursor)
    db.commit()

    failures = []
    for product in products:
        # One transaction per product: a product whose matrix fails leaves
        # nothing behind, and the products that already succeeded stay.
        try:
            product_id, written, withdrawn = import_one(
                cursor, product, args.eur_inr_rate,
                recipe_for(recipes, product))
            db.commit()
            print("  imported %-14s product_id=%s  %d %s row(s)%s"
                  % (product["source_ref"], product_id, written,
                     product["param_mode"].lower(),
                     ", %d withdrawn" % withdrawn if withdrawn else ""))
        except Exception as exc:                # noqa: BLE001 - reported, not hidden
            db.rollback()
            failures.append((product["source_ref"], exc))
            print("  FAILED   %-14s rolled back: %s"
                  % (product["source_ref"], exc))
    print("\n%d imported, %d failed. merchant_enabled stays 0 — release is a "
          "separate decision." % (len(products) - len(failures), len(failures)))
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
