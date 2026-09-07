#!/usr/bin/env python3
"""Seed the catalogue-wide contact-lens minimums and apply them to products.

Dry run by default: prints every row the seed states and every product that
would be updated, writes nothing. ``--apply`` does it, in one transaction.

    python3 scripts/seed_lens_minimums.py            # what would happen
    python3 scripts/seed_lens_minimums.py --apply    # do it

The seed is ``lens_data/contact_lens_min_order.json``; the rule it is checked
against lives in ``lens_minimums.py``. A file that breaks the rule is refused
whole, before anything is written. A product profile is touched only when its
``min_order_model`` names a row of the seed; one that names a model the seed
does not have is reported and left alone.

Connection comes from MYSQL_HOST/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DB, as for
scripts/import_contact_lenses.py.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lens_minimums  # noqa: E402
from import_contact_lenses import connect  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seed", default=lens_minimums.SEED_PATH)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    try:
        rows = lens_minimums.rows(args.seed)
    except lens_minimums.SeedError as exc:
        sys.exit("REFUSED: %s" % exc)
    print("%d models in %s" % (len(rows), args.seed))
    for row in rows:
        print("  %-48s single %2d  both %d+%d%s" % (
            row["model"], row["min_single_eye"], row["min_both_per_eye"],
            row["min_both_per_eye"],
            "  (exception: %s)" % row["exception_note"]
            if row["exception_note"] else ""))

    db = connect()
    cursor = db.cursor()
    try:
        lens_minimums.ensure_table(cursor)
        lens_minimums.seed(cursor, rows)
        matched, unmatched = lens_minimums.apply_to_products(
            cursor, apply=args.apply)
        for product_id, model, single, both in matched:
            print("product %s <- %s: single %d, both %d+%d"
                  % (product_id, model, single, both, both))
        for product_id, model in unmatched:
            print("product %s names %r, which the seed does not state — "
                  "left unchanged" % (product_id, model))
        if args.apply:
            db.commit()
            print("APPLIED: %d seed rows, %d products updated"
                  % (len(rows), len(matched)))
        else:
            db.rollback()
            print("DRY RUN: nothing written (re-run with --apply)")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
