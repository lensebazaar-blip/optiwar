#!/usr/bin/env python3
"""Publish approved retouched JPEGs under static/catalog/r2/<SKU>.

The command is a dry run unless --apply is supplied. It writes masters,
derivatives, and their SHA-256 rows to derivative_manifest.tsv; it never
updates the database. embed_helper loads that manifest once at startup, so
restart gunicorn after applying a batch to make the new entries visible.
"""
import argparse
import os
import re
import shutil
import sys
import tempfile

from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import image_pipeline


def _master_pattern(sku):
    return re.compile(
        r"^%s_r2_\d{2,}\.(?:jpe?g|png|webp|avif)$" % re.escape(sku),
        re.IGNORECASE)


def _master_paths(sku, image_root):
    directory = os.path.join(image_root, "catalog", "r2", sku)
    pattern = _master_pattern(sku)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(
        os.path.join(directory, name)
        for name in names
        if pattern.fullmatch(name)
        and os.path.isfile(os.path.join(directory, name)))


def _family_paths(master_path):
    paths = [master_path]
    directory = os.path.join(os.path.dirname(master_path), "derivatives")
    stem = os.path.splitext(os.path.basename(master_path))[0]
    try:
        names = os.listdir(directory)
    except OSError:
        return paths
    paths.extend(
        os.path.join(directory, name)
        for name in sorted(names)
        if name.startswith(stem + "-")
        and os.path.isfile(os.path.join(directory, name)))
    return paths


def _validate_source(path):
    if not os.path.isfile(path):
        raise ValueError("source file does not exist: %s" % path)
    try:
        with Image.open(path) as image:
            if image.format != "JPEG":
                raise ValueError("source file must be a JPEG: %s" % path)
            image.verify()
    except (OSError, ValueError) as exc:
        raise ValueError("invalid JPEG source %s: %s" % (path, exc)) from exc


def publish(sku, files, image_root, apply=False, replace=False, output=None):
    output = output or sys.stdout
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", sku):
        raise ValueError("invalid SKU: %s" % sku)
    files = [os.path.abspath(path) for path in files]
    if not files:
        raise ValueError("at least one source file is required")
    if len(set(map(os.path.realpath, files))) != len(files):
        raise ValueError("source files must be unique")
    for path in files:
        _validate_source(path)

    image_root = os.path.abspath(image_root)
    catalog_root = os.path.join(image_root, "catalog")
    target_dir = os.path.join(catalog_root, "r2", sku)
    manifest_path = os.path.join(catalog_root, "derivative_manifest.tsv")
    existing_masters = _master_paths(sku, image_root)
    if existing_masters and not replace:
        raise ValueError(
            "%s already has R2 masters; pass --replace to replace them" % sku)
    existing_files = {
        path for master in existing_masters for path in _family_paths(master)
    }

    with tempfile.TemporaryDirectory(prefix="publish-r2-") as staging:
        staged_files = []
        for index, source in enumerate(files, 1):
            filename = "%s_r2_%02d.jpg" % (sku, index)
            staged_master = os.path.join(staging, filename)
            shutil.copy2(source, staged_master)
            derivatives = image_pipeline.build_derivatives(staged_master)
            staged_files.append(
                (staged_master, os.path.join(target_dir, filename)))
            staged_files.extend(
                (derivative, os.path.join(
                    target_dir, "derivatives", os.path.basename(derivative)))
                for derivative in derivatives)

        label = "WRITE" if apply else "DRY RUN WRITE"
        for _, destination in staged_files:
            print("%s %s" % (label, destination), file=output)
        print("%s %s" % (label, manifest_path), file=output)

        if not apply:
            return 0

        written = []
        for source, destination in staged_files:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(source, destination)
            written.append(destination)

        rows = image_pipeline.manifest_rows(written, catalog_root)
        image_pipeline.merge_manifest(
            manifest_path, rows,
            replace_prefix="r2/%s/" % sku if replace else None)

        new_paths = set(written)
        for path in sorted(existing_files - new_paths):
            if os.path.isfile(path):
                os.remove(path)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sku", required=True)
    parser.add_argument("--files", nargs="+", required=True, metavar="FILE")
    parser.add_argument("--image-root", required=True, metavar="PATH")
    parser.add_argument("--apply", action="store_true",
                        help="write the masters, derivatives, and manifest")
    parser.add_argument("--replace", action="store_true",
                        help="replace an existing R2 photo set")
    args = parser.parse_args(argv)
    try:
        return publish(
            args.sku, args.files, args.image_root,
            apply=args.apply, replace=args.replace)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
