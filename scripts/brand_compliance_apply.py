#!/usr/bin/env python3
"""Take trademark-risk frames off sale and give risky names neutral ones.

The lists live in brand_compliance.py (HELD and RENAME). This applies them to
the products table so the database agrees with the code backstop.

    # look, change nothing (default)
    python3 scripts/brand_compliance_apply.py

    # do it
    python3 scripts/brand_compliance_apply.py --apply

    # also move the held frames' photos out of the public static folder
    python3 scripts/brand_compliance_apply.py --apply --quarantine /var/optiwar/quarantine/brand-hold

Per held product:  sell_on_in -> 0, sell_on_com -> 0 (page 404s, gone from
listings, search, feeds and sitemaps on both sites). Stock and order history
are not touched.

Per renamed product:  product_name, product_slug and product_details get the
new name. The old URL 301s to the new one on its own (the product page
redirects any slug that does not match the product id).

--apply first writes a restore SQL file with the exact prior values, so the
change can be undone. --quarantine moves files (never deletes them) and writes
the list of moves next to the restore file.

Connection comes from MYSQL_HOST/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DB, as for
gunicorn:

    set -a; . /etc/optiwar.env; set +a
"""

import argparse
import datetime
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import brand_compliance as bc  # noqa: E402

STATIC = os.path.join(ROOT, "static")


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def sql_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def connect():
    import pymysql
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "localhost"),
        user=os.environ.get("MYSQL_USER", ""),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DB", ""),
        cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def load(cur, ids):
    if not ids:
        return {}
    cur.execute("SELECT product_id, product_code, product_name, product_slug, product_details, "
                "product_image, sell_on_in, sell_on_com FROM products WHERE product_id IN (%s)"
                % ",".join(["%s"] * len(ids)), tuple(ids))
    return {int(r["product_id"]): r for r in cur.fetchall()}


def plan(rows):
    """[(product_id, {column: (old, new)})] for every change still to make."""
    changes = []
    for pid in sorted(bc.HELD):
        row = rows.get(pid)
        if not row:
            continue
        diff = {c: (row[c], 0) for c in ("sell_on_in", "sell_on_com") if int(row[c] or 0) != 0}
        if diff:
            changes.append((pid, diff))
    for pid, (_sku, old, new) in sorted(bc.RENAME.items()):
        row = rows.get(pid)
        if not row:
            continue
        diff = {}
        if row["product_name"] != new:
            diff["product_name"] = (row["product_name"], new)
        if row["product_slug"] != slugify(new):
            diff["product_slug"] = (row["product_slug"], slugify(new))
        details = row.get("product_details") or ""
        current = row["product_name"] or old
        if details and (current in details or old in details):
            diff["product_details"] = (details, details.replace(current, new).replace(old, new))
        if diff:
            changes.append((pid, diff))
    return changes


def image_paths(row):
    out = []
    for part in (row.get("product_image") or "").split(","):
        part = part.strip().lstrip("/")
        if not part or part.upper() == "NULL":
            continue
        rel = part[len("static/"):] if part.startswith("static/") else part
        out.append(rel)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--quarantine", metavar="DIR",
                    help="move the held frames' photos from static/ into DIR")
    ap.add_argument("--by", default=os.environ.get("USER", "ops"))
    args = ap.parse_args(argv)

    db = connect()
    cur = db.cursor()
    ids = sorted(set(bc.HELD) | set(bc.RENAME))
    rows = load(cur, ids)
    missing = [i for i in ids if i not in rows]
    changes = plan(rows)

    print("held: %d, renamed: %d, not in this database: %s"
          % (len(bc.HELD), len(bc.RENAME), missing or "none"))
    for pid, diff in changes:
        sku = (bc.HELD.get(pid) or bc.RENAME.get(pid))[0]
        for col, (old, new) in diff.items():
            shown_old = (str(old)[:60] + "...") if len(str(old)) > 60 else old
            shown_new = (str(new)[:60] + "...") if len(str(new)) > 60 else new
            print("  %s pid %s  %s: %r -> %r" % (sku, pid, col, shown_old, shown_new))
    if not changes:
        print("database already matches brand_compliance.py")

    moves = []
    if args.quarantine:
        for pid in sorted(bc.HELD):
            if pid in rows:
                for rel in image_paths(rows[pid]):
                    src = os.path.join(STATIC, rel)
                    if os.path.isfile(src):
                        moves.append((src, os.path.join(args.quarantine, rel)))
        print("photos to move: %d" % len(moves))

    if not args.apply:
        print("dry run: nothing written. Add --apply to make these changes.")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    restore = os.path.join(os.getcwd(), "brand_compliance_restore_%s.sql" % stamp)
    with open(restore, "w") as fh:
        fh.write("-- restore for brand_compliance_apply %s by %s\n" % (stamp, args.by))
        for pid, diff in changes:
            sets = ", ".join("%s=%s" % (c, sql_literal(old)) for c, (old, _new) in diff.items())
            fh.write("UPDATE products SET %s WHERE product_id=%d;\n" % (sets, pid))
    try:
        for pid, diff in changes:
            sets = ", ".join("%s=%%s" % c for c in diff)
            cur.execute("UPDATE products SET %s WHERE product_id=%%s" % sets,
                        tuple(new for _old, new in diff.values()) + (pid,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    print("applied %d product changes; restore SQL: %s" % (len(changes), restore))

    if moves:
        log = os.path.join(os.getcwd(), "brand_compliance_moves_%s.txt" % stamp)
        with open(log, "w") as fh:
            for src, dst in moves:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                fh.write("%s\t%s\n" % (src, dst))
        print("moved %d photos; list: %s" % (len(moves), log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
