#!/usr/bin/env python3
"""Build a product's commerce masters and their responsive ladder.

Dry run by default. The recipe next to this repo says which photograph becomes
which view; nothing about the package is invented (see image_pipeline).

    # what would be written
    python3 scripts/build_product_images.py --recipe image_recipes/PRECISION1.json \
        --source /var/lib/optiwar-media/source/PRECISION1

    # do it
    python3 scripts/build_product_images.py --recipe image_recipes/PRECISION1.json \
        --source /var/lib/optiwar-media/source/PRECISION1 \
        --catalog-root /var/www/flaskr/static/catalog --apply

Writes, under ``--catalog-root``:

    <catalog_dir>/<code>.jpg                 the square sRGB master
    <catalog_dir>/derivatives/<code>-<w>.<f> AVIF/WebP/JPEG ladder
    derivative_manifest.tsv                  content hashes embed_helper reads

and prints the image records the importer stores in contact_lens_images, so
the PDP, JSON-LD, the sitemap and the merchant feed all name the same files.
Building images releases nothing: merchant_enabled is a separate decision.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import image_pipeline  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--recipe", required=True)
    ap.add_argument("--source", required=True,
                    help="directory of the immutable original photographs")
    ap.add_argument("--catalog-root", default="static/catalog",
                    help="the catalog root masters and derivatives live under")
    ap.add_argument("--records",
                    help="write the image records to this JSON file")
    ap.add_argument("--apply", action="store_true", help="write; else dry run")
    args = ap.parse_args()

    recipe = image_pipeline.load_recipe(args.recipe)
    records, written, warnings = image_pipeline.build_product(
        recipe, args.source, args.catalog_root, apply=args.apply)

    print("%s: %d view(s), %d file(s) %s"
          % (recipe["product"], len(records), len(written),
             "written" if args.apply else "would be written"))
    for record in records:
        print("  %-13s %-9s %s%s"
              % (record["code"], record["view"], record["path"],
                 "  PRIMARY" if record["is_primary"] else ""))
    for level, message in warnings:
        print("  %-5s %s" % (level, message))

    if args.apply:
        manifest = os.path.join(args.catalog_root, "derivative_manifest.tsv")
        total = image_pipeline.merge_manifest(
            manifest, image_pipeline.manifest_rows(written, args.catalog_root))
        print("  manifest %s now has %d entries" % (manifest, total))
    if args.records:
        with open(args.records, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2)
            fh.write("\n")
        print("  records %s" % args.records)

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
    if any(level == "BLOCK" for level, _ in warnings):
        sys.exit(1)


if __name__ == "__main__":
    main()
