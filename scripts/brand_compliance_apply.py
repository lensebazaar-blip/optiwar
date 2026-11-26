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
import re
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
ROUND1_RESTORE = "/root/brand_compliance_restore_20261123-163137.json"
RESTORABLE_COLUMNS = {
    "sell_on_in", "sell_on_com", "show_in_listings", "product_name",
    "product_slug", "product_details", "product_category_browline",
    "product_category_square", "description_seo", "product_image",
    "product_quantity",
}


def connect():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "localhost"),
        user=os.environ.get("MYSQL_USER", ""),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DB", ""),
        cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def _ids():
    return sorted(
        set(brand_compliance.blocked_ids())
        | set(brand_compliance.RELEASED)
        | set(brand_compliance.RENAMED)
        | set(brand_compliance.PHOTO_REPLACED)
        | set(brand_compliance.PHOTO_REMOVE)
        | set(brand_compliance.STOCK_SET))


def _load_rows(cursor):
    ids = _ids()
    marks = ",".join(["%s"] * len(ids))
    cursor.execute(
        "SELECT product_id, product_code, product_name, product_slug, "
        "product_details, description_seo, product_quantity, "
        "sell_on_in, sell_on_com, show_in_listings, "
        "product_category_browline, product_category_clubmaster, "
        "product_category_square, product_category_wayfarer, product_image "
        "FROM products WHERE product_id IN (%s) "
        "OR COALESCE(product_category_clubmaster,0)=1 "
        "OR COALESCE(product_category_wayfarer,0)=1 "
        "ORDER BY product_id FOR UPDATE" % marks, tuple(ids))
    return list(cursor.fetchall() or ())


def _previous_stock_updates(restore_dir):
    try:
        names = os.listdir(restore_dir)
    except FileNotFoundError:
        return set()
    product_ids = set()
    for name in names:
        if (not name.startswith("brand_compliance_restore_")
                or not name.endswith(".json")):
            continue
        path = os.path.join(restore_dir, name)
        with open(path, encoding="utf-8") as stream:
            record = json.load(stream)
        for product_id, changes in record.get("updates", {}).items():
            if "product_quantity" in changes:
                product_ids.add(int(product_id))
    return product_ids


def _changes(row, relist_ready=True, stock_already_applied=False):
    product_id = int(row["product_id"])
    changes = {}
    stock = brand_compliance.STOCK_SET.get(product_id)
    if (stock and not stock_already_applied
            and row.get("product_quantity") is not None):
        if int(row["product_quantity"]) == stock["from"]:
            changes["product_quantity"] = stock["to"]
    if product_id in brand_compliance.blocked_ids():
        for column in ("sell_on_in", "sell_on_com", "show_in_listings"):
            if int(row.get(column) or 0) != 0:
                changes[column] = 0
    elif (product_id in brand_compliance.RELEASED
            and product_id not in brand_compliance.PHOTO_PENDING
            and relist_ready):
        for column in ("sell_on_in", "sell_on_com"):
            if int(row.get(column) or 0) != 1:
                changes[column] = 1
    renamed = brand_compliance.RENAMED.get(product_id)
    if renamed:
        name = renamed[1]
        slug = brand_compliance.slugify(name)
        if row.get("product_name") != name:
            changes["product_name"] = name
        if row.get("product_slug") != slug:
            changes["product_slug"] = slug
        # the stored description opens with the old name ("STAR WARS BO76 - ...")
        details = row.get("product_details")
        cleaned = brand_compliance.scrub_text(details, product_id)
        if details and cleaned != details:
            changes["product_details"] = cleaned
        seo = row.get("description_seo")
        cleaned_seo = brand_compliance.scrub_text(seo, product_id)
        if seo and cleaned_seo != seo:
            changes["description_seo"] = cleaned_seo
    if (int(row.get("product_category_clubmaster") or 0) == 1
            and int(row.get("product_category_browline") or 0) != 1):
        changes["product_category_browline"] = 1
    if (int(row.get("product_category_wayfarer") or 0) == 1
            and int(row.get("product_category_square") or 0) != 1):
        changes["product_category_square"] = 1
    return changes


def _image_paths(row):
    product_id = int(row["product_id"])
    paths = []
    for raw in (row.get("product_image") or "").split(","):
        relative = raw.strip().replace("\\", "/")
        if not relative:
            continue
        relative = os.path.normpath(relative)
        if (relative in (".", "..") or relative.startswith("../")
                or os.path.isabs(relative) or "://" in relative):
            raise ValueError("unsafe product_image path for %s" % product_id)
        paths.append(relative)
    return paths


def _approved_r2(sku, image_root):
    directory = os.path.join(image_root, "catalog", "r2", sku)
    pattern = re.compile(
        r"^%s_r2_\d{2,}\.(?:jpe?g|png|webp|avif)$" % re.escape(sku),
        re.IGNORECASE)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(
        os.path.join("catalog", "r2", sku, name).replace(os.sep, "/")
        for name in names
        if pattern.fullmatch(name)
        and os.path.isfile(os.path.join(directory, name)))


def _photo_replacement_plan(row, image_root):
    product_id = int(row["product_id"])
    original = row.get("product_image") or ""
    sku = brand_compliance.PHOTO_REPLACED.get(product_id)
    if not sku:
        return {"ready": True, "image_value": original, "approved": [],
                "old_public": []}

    approved = _approved_r2(sku, image_root)
    if len(approved) < 2:
        return {"ready": False, "image_value": None, "approved": approved,
                "old_public": []}

    old_public = [
        relative for relative in _image_paths(row)
        if not relative.startswith("catalog/r2/")
    ]
    return {
        "ready": True,
        "image_value": ",".join(approved),
        "approved": approved,
        "old_public": old_public,
    }


def _photo_remove_plan(row):
    product_id = int(row["product_id"])
    filenames = list(dict.fromkeys(
        brand_compliance.PHOTO_REMOVE.get(product_id, ())))
    original = row.get("product_image") or ""
    if not filenames:
        return {
            "image_value": original,
            "paths": [],
            "missing": [],
            "ambiguous": [],
        }

    paths = _image_paths(row)
    entries = [entry.strip() for entry in original.split(",") if entry.strip()]
    matches = {
        filename: [
            index for index, path in enumerate(paths)
            if os.path.basename(path) == filename
        ]
        for filename in filenames
    }
    missing = sorted(
        filename for filename, found in matches.items() if not found)
    ambiguous = sorted(
        filename for filename, found in matches.items() if len(found) > 1)
    removed = {index for found in matches.values() for index in found}
    remaining = [entry for index, entry in enumerate(entries)
                 if index not in removed]
    return {
        "image_value": ",".join(remaining) if not ambiguous else original,
        "paths": [
            paths[index]
            for filename in filenames
            for index in matches[filename]
        ],
        "missing": missing,
        "ambiguous": ambiguous,
    }


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


def _round1_record(path):
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def _same_relative_suffix(path, relative):
    path = os.path.normpath(str(path)).replace("\\", "/")
    relative = os.path.normpath(relative).replace("\\", "/")
    return path == relative or path.endswith("/" + relative)


def _find_quarantined(relative, quarantine_base, record):
    for move in record.get("image_moves", []):
        candidate = move.get("quarantined")
        if (candidate and _within(quarantine_base, candidate)
                and _same_relative_suffix(move.get("source", ""), relative)
                and os.path.isfile(candidate)):
            return candidate
    try:
        directories = sorted(os.listdir(quarantine_base), reverse=True)
    except OSError:
        return None
    for directory in directories:
        candidate = os.path.join(quarantine_base, directory, relative)
        if _within(quarantine_base, candidate) and os.path.isfile(candidate):
            return candidate
    return None


def _republish_plan(row, image_root, quarantine_base, record):
    product_id = int(row["product_id"])
    original = row.get("product_image") or ""
    if (product_id not in brand_compliance.RELEASED
            or product_id in brand_compliance.PHOTO_PENDING):
        return {"ready": True, "image_value": original, "copies": [],
                "old_public": [], "missing": []}

    sku = brand_compliance.RELEASED[product_id]
    paths = _image_paths(row)
    removed_filenames = set(brand_compliance.PHOTO_REMOVE.get(product_id, ()))
    approved = [
        relative for relative in _approved_r2(sku, image_root)
        if os.path.basename(relative) not in removed_filenames
    ]
    if approved:
        return {
            "ready": True,
            "image_value": ",".join(approved),
            "copies": [],
            "old_public": [
                relative for relative in paths
                if not relative.startswith("catalog/r2/")
            ],
            "missing": [],
        }
    if not paths:
        return {"ready": False, "image_value": None, "copies": [],
                "old_public": [], "missing": [{"relative_path": ""}]}

    targets, copies, old_public, missing = [], [], [], []
    for index, relative in enumerate(paths, 1):
        if relative.startswith(os.path.join("catalog", "r2") + os.sep):
            if os.path.isfile(os.path.join(image_root, relative)):
                targets.append(relative)
            else:
                missing.append({"relative_path": relative})
            continue

        public_source = os.path.join(image_root, relative)
        old_public.append(relative)

        extension = os.path.splitext(relative)[1] or ".jpg"
        target_relative = os.path.join(
            "catalog", "r2", sku, "%s_r2_%02d%s" % (sku, index, extension))
        target = os.path.join(image_root, target_relative)
        if os.path.isfile(target):
            targets.append(target_relative)
            continue

        source = public_source if os.path.isfile(public_source) else None
        if not source:
            source = _find_quarantined(relative, quarantine_base, record)
        if not source:
            missing.append({
                "relative_path": relative,
                "expected_quarantine": os.path.join(
                    quarantine_base, "<timestamp>", relative),
            })
            continue
        copies.append({"source": source, "published": target})
        targets.append(target_relative)

    ready = not missing and len(targets) == len(paths)
    image_value = ",".join(targets) if ready else None
    return {"ready": ready, "image_value": image_value, "copies": copies,
            "old_public": old_public, "missing": missing}


def _image_family_paths(relative, root):
    paths = [relative]
    directory = os.path.join(
        os.path.dirname(relative), "derivatives")
    derivative_dir = os.path.join(root, directory)
    stem = os.path.splitext(os.path.basename(relative))[0]
    try:
        names = os.listdir(derivative_dir)
    except OSError:
        return paths
    for name in sorted(names):
        if name.startswith(stem + "-"):
            candidate = os.path.join(derivative_dir, name)
            if os.path.isfile(candidate):
                paths.append(os.path.join(directory, name))
    return paths


def _quarantine_plan(rows, extra_paths, root, quarantine_dir,
                     quarantine_base, record, exact_paths=()):
    blocked_paths = []
    seen = set()
    for row in rows:
        if int(row["product_id"]) not in brand_compliance.blocked_ids():
            continue
        for relative in _image_paths(row):
            for family_path in _image_family_paths(relative, root):
                if family_path not in seen:
                    blocked_paths.append(family_path)
                    seen.add(family_path)
    extra = []
    for relative in extra_paths:
        for family_path in _image_family_paths(relative, root):
            if family_path not in seen:
                extra.append(family_path)
                seen.add(family_path)
    for relative in exact_paths:
        for family_path in _image_family_paths(relative, root):
            if family_path not in seen:
                extra.append(family_path)
                seen.add(family_path)

    def plan_paths(paths):
        planned, already, missing = [], [], []
        for relative in paths:
            source = os.path.join(root, relative)
            destination = os.path.join(quarantine_dir, relative)
            if os.path.isfile(source):
                if os.path.exists(destination):
                    raise FileExistsError(
                        "quarantine destination exists: %s" % destination)
                planned.append({"source": source, "quarantined": destination})
            elif _find_quarantined(relative, quarantine_base, record):
                already.append({"source": source, "relative_path": relative})
            else:
                missing.append({"source": source, "relative_path": relative})
        return planned, already, missing

    return plan_paths(blocked_paths), plan_paths(extra)


def _move_images(plans, moved=None):
    moved = moved if moved is not None else []
    for move in plans:
        source, destination = move["source"], move["quarantined"]
        if not os.path.isfile(source):
            raise FileNotFoundError(
                "image disappeared before quarantine: %s" % source)
        if os.path.exists(destination):
            raise FileExistsError("quarantine destination exists: %s" % destination)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        move = {"source": source, "quarantined": destination}
        moved.append(move)
        shutil.move(source, destination)
        print("QUARANTINE %s -> %s" % (source, destination))
    return moved


def _copy_images(plans, copied=None):
    copied = copied if copied is not None else []
    for copy in plans:
        source, destination = copy["source"], copy["published"]
        if not os.path.isfile(source):
            raise FileNotFoundError("republish source disappeared: %s" % source)
        if os.path.exists(destination):
            raise FileExistsError("republish destination exists: %s" % destination)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        published = {"source": source, "published": destination}
        copied.append(published)
        shutil.copy2(source, destination)
        print("REPUBLISH %s -> %s" % (source, destination))
    return copied


def _remove_copies(copies):
    for copy in reversed(copies):
        published = copy["published"]
        if os.path.exists(published):
            os.remove(published)


def _print_summary(summary):
    print(
        "SUMMARY off sale=%d relisted=%d renamed=%d seo-scrubbed=%d "
        "photos to quarantine=%d photos to republish=%d stock updates=%d "
        "photos replaced=%d"
        % (summary["off_sale"], summary["relisted"], summary["renamed"],
           summary["seo_scrubbed"], summary["photos_to_quarantine"],
           summary["photos_to_republish"], summary["stock_updates"],
           summary["photos_replaced"]))


def run(db, args, image_root=None, quarantine_base=None, restore_dir=None,
        round1_restore=None):
    image_root = image_root or IMAGE_ROOT
    quarantine_base = quarantine_base or QUARANTINE_BASE
    restore_dir = restore_dir or RESTORE_DIR
    round1_restore = round1_restore or ROUND1_RESTORE
    previous_stock_updates = _previous_stock_updates(restore_dir)
    cursor = db.cursor()
    moved = []
    copied = []
    restore_path = None
    try:
        rows = _load_rows(cursor)
        record = _round1_record(round1_restore)
        photo_removals = {
            int(row["product_id"]): _photo_remove_plan(row)
            for row in rows
        }
        row_ids = set(photo_removals)
        missing_photo_rows = sorted(
            product_id for product_id in brand_compliance.PHOTO_REMOVE
            if product_id not in row_ids)
        photo_remove_issues = [
            {"product_id": product_id, "filename": filename,
             "issue": "ambiguous"}
            for product_id, plan in photo_removals.items()
            for filename in plan["ambiguous"]
        ]
        if args.apply and (missing_photo_rows or photo_remove_issues):
            for product_id in missing_photo_rows:
                print("MISSING PHOTO_REMOVE product %s" % product_id)
            for item in photo_remove_issues:
                print("%s PHOTO_REMOVE %s for product %s" % (
                    item["issue"].upper(), item["filename"], item["product_id"]))
            raise RuntimeError(
                "cannot apply: PHOTO_REMOVE must match exactly one product image")

        photo_remove_paths = [
            path for plan in photo_removals.values() for path in plan["paths"]
        ]
        working_rows = []
        for row in rows:
            working = dict(row)
            working["product_image"] = photo_removals[
                int(row["product_id"])]["image_value"]
            working_rows.append(working)
        republish = {
            int(row["product_id"]): _republish_plan(
                row, image_root, quarantine_base, record)
            for row in working_rows
        }
        photo_replacements = {
            int(row["product_id"]): _photo_replacement_plan(row, image_root)
            for row in rows
        }
        missing_republish = [
            {"product_id": product_id, **item}
            for product_id, plan in republish.items()
            for item in plan["missing"]
        ]
        if args.apply and missing_republish:
            for item in missing_republish:
                print("MISSING republish image for product %s: %s" % (
                    item["product_id"], item["relative_path"]))
            raise RuntimeError(
                "cannot apply: released products are missing republishable images")

        blocked_rows = [
            row for row in rows
            if int(row["product_id"]) in brand_compliance.blocked_ids()
        ]
        old_public = [
            relative
            for plan in republish.values()
            for relative in plan["old_public"]
        ]
        old_public.extend(
            relative
            for plan in photo_replacements.values()
            if plan["ready"]
            for relative in plan["old_public"])
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        quarantine_dir = os.path.join(quarantine_base, timestamp)
        display_quarantine_dir = (
            quarantine_dir if args.apply
            else os.path.join(quarantine_base, "<timestamp>"))
        blocked_plan, release_plan = _quarantine_plan(
            blocked_rows, old_public, image_root, display_quarantine_dir,
            quarantine_base, record, exact_paths=photo_remove_paths)
        blocked_moves, blocked_already, blocked_missing = blocked_plan
        release_moves, release_already, release_missing = release_plan
        quarantine_plans = blocked_moves + release_moves
        already_quarantined = blocked_already + release_already
        missing_images = blocked_missing + release_missing

        before = {}
        changed_rows = []
        summary = {
            "off_sale": 0,
            "relisted": 0,
            "renamed": 0,
            "seo_scrubbed": 0,
            "stock_updates": 0,
            "photos_to_quarantine": len(quarantine_plans),
            "photos_to_republish": sum(
                len(plan["copies"]) for plan in republish.values()),
            "photos_replaced": 0,
        }
        for row in rows:
            product_id = int(row["product_id"])
            plan = republish[product_id]
            replacement = photo_replacements[product_id]
            stock = brand_compliance.STOCK_SET.get(product_id)
            stock_already_applied = product_id in previous_stock_updates
            if stock and stock_already_applied:
                print("STOCK ALREADY APPLIED %s" % stock["sku"])
            if (product_id in brand_compliance.PHOTO_REPLACED
                    and not replacement["ready"]):
                print("MISSING approved R2 photos for %s (found %d, need at least 2)" % (
                    brand_compliance.PHOTO_REPLACED[product_id],
                    len(replacement["approved"])))
                changes = {}
            else:
                changes = _changes(
                    row, relist_ready=plan["ready"],
                    stock_already_applied=stock_already_applied)
                if (product_id in brand_compliance.PHOTO_REPLACED
                        and replacement["image_value"] is not None
                        and replacement["image_value"]
                        != (row.get("product_image") or "")):
                    changes["product_image"] = replacement["image_value"]
                    print("REPLACE PHOTOS %s old=%d new=%d" % (
                        brand_compliance.PHOTO_REPLACED[product_id],
                        len(_image_paths(row)),
                        len(replacement["approved"])))
                    summary["photos_replaced"] += 1
            if (plan["ready"] and plan["image_value"] is not None
                    and plan["image_value"] != (row.get("product_image") or "")):
                changes["product_image"] = plan["image_value"]
            if any(changes.get(column) == 0
                   and int(row.get(column) or 0) != 0
                   for column in ("sell_on_in", "sell_on_com", "show_in_listings")):
                summary["off_sale"] += 1
            if any(changes.get(column) == 1
                   and int(row.get(column) or 0) != 1
                   for column in ("sell_on_in", "sell_on_com")):
                summary["relisted"] += 1
            if "product_name" in changes or "product_slug" in changes:
                summary["renamed"] += 1
            if "description_seo" in changes:
                summary["seo_scrubbed"] += 1
            if "product_quantity" in changes:
                summary["stock_updates"] += 1
            if (stock and not stock_already_applied
                    and row.get("product_quantity") is not None):
                quantity = int(row["product_quantity"])
                if quantity not in (stock["from"], stock["to"]):
                    print("STOCK GUARD SKIP %s product_quantity=%s expected=%s" % (
                        stock["sku"], quantity, stock["from"]))
            if not changes:
                continue
            before[str(product_id)] = {
                column: row.get(column) for column in changes
            }
            changed_rows.append((row, changes))
            detail = ", ".join("%s=%r" % (key, value)
                               for key, value in changes.items())
            print("%s %s (%s)" % (
                "APPLY" if args.apply else "DRY RUN",
                row.get("product_code") or product_id, detail))
        if not args.apply:
            print("DRY RUN quarantine root: %s/<timestamp>" % quarantine_base)
            for product_id, plan in photo_removals.items():
                for path in plan["paths"]:
                    print("DRY RUN PHOTO REMOVE %s %s" % (product_id, path))
                for filename in plan["missing"]:
                    print("ALREADY REMOVED PHOTO_REMOVE %s for product %s" % (
                        filename, product_id))
            for product_id in missing_photo_rows:
                print("MISSING PHOTO_REMOVE product %s" % product_id)
            for item in photo_remove_issues:
                print("%s PHOTO_REMOVE %s for product %s" % (
                    item["issue"].upper(), item["filename"], item["product_id"]))
            for move in quarantine_plans:
                print("DRY RUN QUARANTINE %s -> %s" % (
                    move["source"], move["quarantined"]))
            for item in already_quarantined:
                print("ALREADY QUARANTINED %s" % item["relative_path"])
            for item in missing_images:
                print("MISSING image %s" % item["source"])
            for plan in republish.values():
                for copy in plan["copies"]:
                    print("DRY RUN REPUBLISH %s -> %s" % (
                        copy["source"], copy["published"]))
            for item in missing_republish:
                print("MISSING republish image for product %s: %s" % (
                    item["product_id"], item["relative_path"]))
            _print_summary(summary)
            if (not changed_rows and not quarantine_plans
                    and not any(plan["copies"] for plan in republish.values())):
                print("No changes.")
            db.rollback()
            return 0

        if (blocked_moves or release_moves) and not getattr(args, "quarantine", False):
            raise RuntimeError(
                "public old image paths require --quarantine before applying")

        for row, changes in changed_rows:
            _apply_row(cursor, int(row["product_id"]), changes)

        all_copies = [
            copy for plan in republish.values() for copy in plan["copies"]
        ]
        image_copies = _copy_images(all_copies, copied)
        image_moves = _move_images(quarantine_plans, moved)
        if not changed_rows and not image_moves and not image_copies:
            db.rollback()
            print("No changes.")
            return 0

        restore_path = os.path.join(
            restore_dir, "brand_compliance_restore_%s.json" % timestamp)
        os.makedirs(restore_dir, exist_ok=True)
        _write_restore(restore_path, {
            "created_at": timestamp,
            "updates": before,
            "applied_values": {
                str(int(row["product_id"])): {
                    "product_quantity": changes["product_quantity"],
                }
                for row, changes in changed_rows
                if "product_quantity" in changes
            },
            "image_moves": image_moves,
            "missing_images": missing_images,
            "image_copies": image_copies,
        })
        db.commit()
        _print_summary(summary)
        print("Restore record: %s" % restore_path)
        return 0
    except Exception:
        db.rollback()
        for move in reversed(moved):
            if os.path.exists(move["quarantined"]) and not os.path.exists(move["source"]):
                os.makedirs(os.path.dirname(move["source"]), exist_ok=True)
                shutil.move(move["quarantined"], move["source"])
        _remove_copies(copied)
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
    for copy in record.get("image_copies", []):
        source, published = copy["source"], copy["published"]
        if (not (_within(image_root, source) or _within(quarantine_base, source))
                or not _within(image_root, published)):
            raise ValueError("restore file contains an unsafe copied image path")
    cursor = db.cursor()
    try:
        for product_id, values in record.get("updates", {}).items():
            columns = [column for column in values if column in RESTORABLE_COLUMNS]
            if len(columns) != len(values):
                raise ValueError("restore file contains an unsupported column")
            if "product_quantity" in columns:
                cursor.execute(
                    "SELECT product_quantity FROM products "
                    "WHERE product_id=%s FOR UPDATE", (int(product_id),))
                current_row = cursor.fetchone()
                if isinstance(current_row, dict):
                    current_quantity = current_row.get("product_quantity")
                elif current_row is None:
                    current_quantity = None
                else:
                    current_quantity = current_row[0]
                applied_quantity = record.get(
                    "applied_values", {}).get(
                        str(product_id), {}).get("product_quantity")
                if applied_quantity is None:
                    stock = brand_compliance.STOCK_SET.get(int(product_id))
                    if stock:
                        applied_quantity = stock["to"]
                if current_quantity != applied_quantity:
                    print(
                        "SKIP RESTORE product_quantity %s (now %s, applied %s)"
                        % (product_id, current_quantity, applied_quantity))
                    columns.remove("product_quantity")
            if not columns:
                continue
            assignments = ", ".join("%s=%%s" % column for column in columns)
            params = [values[column] for column in columns] + [int(product_id)]
            cursor.execute("UPDATE products SET %s WHERE product_id=%%s" % assignments,
                           tuple(params))
        removed_copies = []
        for copy in record.get("image_copies", []):
            published = copy["published"]
            if os.path.isfile(published):
                os.remove(published)
                removed_copies.append(copy)
            else:
                print("MISSING published image %s" % published)
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
            len(record.get("updates", {})),
            len(restored) + len(removed_copies)))
        return 0
    except Exception:
        db.rollback()
        for source, destination in reversed(locals().get("restored", [])):
            if os.path.exists(source) and not os.path.exists(destination):
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.move(source, destination)
        for copy in reversed(locals().get("removed_copies", [])):
            source = copy["source"]
            if not os.path.isfile(source):
                source = next((
                    move["quarantined"]
                    for move in record.get("image_moves", [])
                    if move["source"] == copy["source"]
                    and os.path.isfile(move["quarantined"])
                ), source)
            published = copy["published"]
            if os.path.isfile(source) and not os.path.exists(published):
                os.makedirs(os.path.dirname(published), exist_ok=True)
                shutil.copy2(source, published)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="commit the database changes; default is dry run")
    mode.add_argument("--restore", metavar="FILE",
                      help="restore database values and quarantined files")
    parser.add_argument("--quarantine", action="store_true",
                        help="quarantine public blocked/old images during apply")
    parser.add_argument("--image-root", metavar="PATH", default=IMAGE_ROOT,
                        help="static root containing product images "
                             "(default: %(default)s)")
    parser.add_argument("--round1-restore", metavar="FILE",
                        default=ROUND1_RESTORE,
                        help="round-one restore record used to locate quarantined photos "
                             "(default: %(default)s)")
    args = parser.parse_args(argv)
    if args.quarantine and not args.apply:
        parser.error("--quarantine is valid only with --apply")
    db = connect()
    try:
        if args.restore:
            return restore(db, args.restore, image_root=args.image_root)
        return run(db, args, image_root=args.image_root,
                   round1_restore=args.round1_restore)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
