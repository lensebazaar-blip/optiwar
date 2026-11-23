#!/usr/bin/env python3
"""Apply or restore the settled Optiwar trademark catalogue changes.

Dry run is the default. An apply writes its restore record before commit:

    python3 scripts/brand_compliance_apply.py
    python3 scripts/brand_compliance_apply.py --apply
    python3 scripts/brand_compliance_apply.py --apply --quarantine
    python3 scripts/brand_compliance_apply.py --restore /root/brand_compliance_restore_<ts>.json
"""
import argparse
import datetime
import json
import os
import shutil
import sys

import pymysql

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import brand_compliance

IMAGE_ROOT = os.path.join(REPO, "static")
QUARANTINE_BASE = "/root/brand_quarantine"
RESTORE_DIR = "/root"
RESTORABLE_COLUMNS = {
    "sell_on_in", "sell_on_com", "show_in_listings", "product_name",
    "product_slug", "product_category_browline", "product_category_square",
}


def connect():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "localhost"),
        user=os.environ.get("MYSQL_USER", ""),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DB", ""),
        cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def _ids():
    return sorted(set(brand_compliance.HELD) | set(brand_compliance.RENAMED))


def _load_rows(cursor):
    ids = _ids()
    marks = ",".join(["%s"] * len(ids))
    cursor.execute(
        "SELECT product_id, product_code, product_name, product_slug, "
        "sell_on_in, sell_on_com, show_in_listings, "
        "product_category_browline, product_category_clubmaster, "
        "product_category_square, product_category_wayfarer, product_image "
        "FROM products WHERE product_id IN (%s) "
        "OR COALESCE(product_category_clubmaster,0)=1 "
        "OR COALESCE(product_category_wayfarer,0)=1 "
        "ORDER BY product_id FOR UPDATE" % marks, tuple(ids))
    return list(cursor.fetchall() or ())


def _changes(row):
    product_id = int(row["product_id"])
    changes = {}
    if product_id in brand_compliance.HELD:
        for column in ("sell_on_in", "sell_on_com", "show_in_listings"):
            if int(row.get(column) or 0) != 0:
                changes[column] = 0
    renamed = brand_compliance.RENAMED.get(product_id)
    if renamed:
        name = renamed[1]
        slug = brand_compliance.slugify(name)
        if row.get("product_name") != name:
            changes["product_name"] = name
        if row.get("product_slug") != slug:
            changes["product_slug"] = slug
    if (int(row.get("product_category_clubmaster") or 0) == 1
            and int(row.get("product_category_browline") or 0) != 1):
        changes["product_category_browline"] = 1
    if (int(row.get("product_category_wayfarer") or 0) == 1
            and int(row.get("product_category_square") or 0) != 1):
        changes["product_category_square"] = 1
    return changes


def _image_paths(row):
    product_id = int(row["product_id"])
    held = brand_compliance.HELD.get(product_id)
    if not held or held[2] != "frame_mark":
        return []
    paths = []
    for raw in (row.get("product_image") or "").split(","):
        relative = raw.strip().replace("\\", "/")
        if not relative:
            continue
        relative = os.path.normpath(relative)
        if relative in (".", "..") or relative.startswith("../") or os.path.isabs(relative):
            raise ValueError("unsafe product_image path for %s" % product_id)
        paths.append(relative)
    return paths


def _apply_row(cursor, product_id, changes):
    if not changes:
        return
    columns = list(changes)
    assignments = ", ".join("%s=%%s" % column for column in columns)
    values = [changes[column] for column in columns]
    values.append(product_id)
    cursor.execute("UPDATE products SET %s WHERE product_id=%%s" % assignments,
                   tuple(values))


def _write_restore(path, record):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _move_images(rows, root, quarantine_dir, moved=None):
    moved = moved if moved is not None else []
    missing = []
    for row in rows:
        for relative in _image_paths(row):
            source = os.path.join(root, relative)
            destination = os.path.join(quarantine_dir, relative)
            if not os.path.isfile(source):
                missing.append({"source": source, "relative_path": relative})
                print("MISSING image %s" % source)
                continue
            if os.path.exists(destination):
                raise FileExistsError("quarantine destination exists: %s" % destination)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            move = {"source": source, "quarantined": destination}
            moved.append(move)
            shutil.move(source, destination)
            print("QUARANTINE %s -> %s" % (source, destination))
    return moved, missing


def run(db, args, image_root=None, quarantine_base=None, restore_dir=None):
    image_root = image_root or IMAGE_ROOT
    quarantine_base = quarantine_base or QUARANTINE_BASE
    restore_dir = restore_dir or RESTORE_DIR
    cursor = db.cursor()
    moved = []
    restore_path = None
    try:
        rows = _load_rows(cursor)
        before = {}
        changed_rows = []
        for row in rows:
            changes = _changes(row)
            if not changes:
                continue
            product_id = int(row["product_id"])
            before[str(product_id)] = {
                column: row.get(column) for column in changes
            }
            changed_rows.append((row, changes))
            detail = ", ".join("%s=%r" % (key, value)
                               for key, value in changes.items())
            print("%s %s (%s)" % (
                "APPLY" if args.apply else "DRY RUN",
                row.get("product_code") or product_id, detail))
        image_rows = [row for row in rows
                      if int(row["product_id"]) in brand_compliance.HELD]
        if not args.apply:
            if getattr(args, "quarantine", False):
                print("DRY RUN quarantine root: %s/<timestamp>" % quarantine_base)
                for row in image_rows:
                    for relative in _image_paths(row):
                        source = os.path.join(image_root, relative)
                        print("DRY RUN QUARANTINE %s -> %s" % (
                            source, os.path.join(quarantine_base, "<timestamp>", relative)))
            if not changed_rows:
                print("No changes.")
            db.rollback()
            return 0

        for row, changes in changed_rows:
            _apply_row(cursor, int(row["product_id"]), changes)

        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        image_moves, missing_images = [], []
        if getattr(args, "quarantine", False):
            quarantine_dir = os.path.join(quarantine_base, timestamp)
            image_moves, missing_images = _move_images(
                image_rows, image_root, quarantine_dir, moved)
        if not changed_rows and not image_moves:
            db.rollback()
            print("No changes.")
            return 0

        restore_path = os.path.join(
            restore_dir, "brand_compliance_restore_%s.json" % timestamp)
        os.makedirs(restore_dir, exist_ok=True)
        _write_restore(restore_path, {
            "created_at": timestamp,
            "updates": before,
            "image_moves": image_moves,
            "missing_images": missing_images,
        })
        db.commit()
        print("Restore record: %s" % restore_path)
        return 0
    except Exception:
        db.rollback()
        for move in reversed(moved):
            if os.path.exists(move["quarantined"]) and not os.path.exists(move["source"]):
                os.makedirs(os.path.dirname(move["source"]), exist_ok=True)
                shutil.move(move["quarantined"], move["source"])
        raise


def _within(root, path):
    root = os.path.realpath(root)
    path = os.path.realpath(path)
    try:
        return os.path.commonpath((root, path)) == root
    except ValueError:
        return False


def restore(db, path, image_root=None, quarantine_base=None):
    image_root = image_root or IMAGE_ROOT
    quarantine_base = quarantine_base or QUARANTINE_BASE
    with open(path, encoding="utf-8") as stream:
        record = json.load(stream)
    for move in record.get("image_moves", []):
        source, destination = move["source"], move["quarantined"]
        if not _within(image_root, source) or not _within(quarantine_base, destination):
            raise ValueError("restore file contains an unsafe image path")
    cursor = db.cursor()
    try:
        for product_id, values in record.get("updates", {}).items():
            columns = [column for column in values if column in RESTORABLE_COLUMNS]
            if len(columns) != len(values):
                raise ValueError("restore file contains an unsupported column")
            if not columns:
                continue
            assignments = ", ".join("%s=%%s" % column for column in columns)
            params = [values[column] for column in columns] + [int(product_id)]
            cursor.execute("UPDATE products SET %s WHERE product_id=%%s" % assignments,
                           tuple(params))
        restored = []
        for move in record.get("image_moves", []):
            source, destination = move["source"], move["quarantined"]
            if not os.path.isfile(destination):
                print("MISSING quarantined image %s" % destination)
                continue
            if os.path.exists(source):
                raise FileExistsError("restore destination exists: %s" % source)
            os.makedirs(os.path.dirname(source), exist_ok=True)
            shutil.move(destination, source)
            restored.append((source, destination))
        db.commit()
        print("Restored %d product row(s) and %d image(s)" % (
            len(record.get("updates", {})), len(restored)))
        return 0
    except Exception:
        db.rollback()
        for source, destination in reversed(locals().get("restored", [])):
            if os.path.exists(source) and not os.path.exists(destination):
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.move(source, destination)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="commit the database changes; default is dry run")
    mode.add_argument("--restore", metavar="FILE",
                      help="restore database values and quarantined files")
    parser.add_argument("--quarantine", action="store_true",
                        help="move held-frame images outside the public static root")
    args = parser.parse_args(argv)
    if args.quarantine and not args.apply:
        parser.error("--quarantine is valid only with --apply")
    db = connect()
    try:
        if args.restore:
            return restore(db, args.restore)
        return run(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
