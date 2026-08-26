#!/usr/bin/env python3
"""Record which vertical a product belongs to, and which storefronts sell it.

The migration gives every existing product ``EYEWEAR`` on both sites, which is
exactly what it was before the columns existed. This is the deliberate second
step: saying that a product is a contact lens, and that contact lenses are sold
on optiwar.com and not on optiwar.in.

    # look, change nothing (default)
    python3 scripts/classify_vertical.py --ids 1005 --vertical CONTACT_LENS --by sudhanshu

    # do it
    python3 scripts/classify_vertical.py --ids 1005 --vertical CONTACT_LENS --by sudhanshu --apply

    # or classify by what the catalogue already says
    python3 scripts/classify_vertical.py --category "Contact Lenses" --vertical CONTACT_LENS --by sudhanshu

Site eligibility follows the vertical unless stated: a lens is .com-only, a
frame is sold on both. ``--sell-on`` overrides that for the one-off case (a
lens deliberately withheld from .com, or eyewear built for one storefront).

Nothing else about the product is touched — not the status, not the quantity,
not the price. A lens is not written off by being classified, and a frame that
was ACTIVE stays ACTIVE.

Per product, inside one transaction:

    product_vertical -> the given vertical
    sell_on_com      -> per vertical, or --sell-on
    sell_on_in       -> per vertical, or --sell-on
    product_status_history += one row recording it, because a product
                             disappearing from a storefront must be explicable

--apply first writes a restore script containing the exact prior values.

Connection comes from MYSQL_HOST/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DB, the same
variables gunicorn runs with, so on the box:

    set -a; . /etc/optiwar.env; set +a
"""
import argparse
import datetime
import os
import sys

import pymysql

VERTICALS = ("EYEWEAR", "CONTACT_LENS")

# Which storefronts sell a vertical, absent an explicit --sell-on. Contact
# lenses are .com-only: .in has no licensing decision behind it yet, and the
# whole point of the flag is that the absence is data rather than a hidden
# condition in six read paths.
DEFAULT_SITES = {
    "EYEWEAR": ("com", "in"),
    "CONTACT_LENS": ("com",),
}

SELECT_COLS = ("product_id, product_code, product_name, product_category, "
               "product_status, product_vertical, sell_on_com, sell_on_in")


def connect():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "localhost"),
        user=os.environ.get("MYSQL_USER", ""),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DB", ""),
        cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def select_products(cur, category, ids):
    if ids:
        marks = ",".join(["%s"] * len(ids))
        cur.execute("SELECT %s FROM products WHERE product_id IN (%s) "
                    "ORDER BY product_id" % (SELECT_COLS, marks), tuple(ids))
    else:
        cur.execute("SELECT %s FROM products WHERE product_category=%%s "
                    "ORDER BY product_id" % SELECT_COLS, (category,))
    return cur.fetchall()


def target(vertical, sell_on):
    sites = tuple(sell_on) if sell_on else DEFAULT_SITES[vertical]
    return {"product_vertical": vertical,
            "sell_on_com": 1 if "com" in sites else 0,
            "sell_on_in": 1 if "in" in sites else 0}


def needs_change(row, want):
    return any(_norm(row[k]) != want[k] for k in want)


def _norm(value):
    if isinstance(value, str):
        return value
    return int(value) if value is not None else None


def restore_script(rows, path):
    lines = ["-- Undo the classification. Restores the exact prior values.",
             "-- Generated %s"
             % datetime.datetime.now().isoformat(timespec="seconds"),
             "START TRANSACTION;"]
    for r in rows:
        lines.append(
            "UPDATE products SET product_vertical='%s', sell_on_com=%s, "
            "sell_on_in=%s WHERE product_id=%s;  -- %s"
            % (r["product_vertical"] or "EYEWEAR",
               int(r["sell_on_com"] or 0), int(r["sell_on_in"] or 0),
               r["product_id"], r["product_code"]))
    lines += ["COMMIT;", ""]
    with open(path, "w") as fh:
        fh.write("\n".join(lines))


def describe(row, want):
    return ("  %-6s %-10s %-28s %s/com=%s/in=%s  ->  %s/com=%s/in=%s"
            % (row["product_id"], row["product_code"],
               (row["product_name"] or "")[:28],
               row["product_vertical"] or "-",
               row["sell_on_com"], row["sell_on_in"],
               want["product_vertical"], want["sell_on_com"],
               want["sell_on_in"]))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--ids", help="comma-separated product_ids")
    group.add_argument("--category", help="every product in this product_category")
    ap.add_argument("--vertical", required=True, choices=VERTICALS)
    ap.add_argument("--by", required=True, help="who authorised it")
    ap.add_argument("--sell-on", default=None,
                    help="comma-separated storefronts (com,in); default follows "
                         "the vertical")
    ap.add_argument("--apply", action="store_true",
                    help="write; otherwise dry run")
    ap.add_argument("--restore-file", default=None,
                    help="where to write the undo script "
                         "(default: ./restore_vertical_<stamp>.sql)")
    args = ap.parse_args()

    sell_on = None
    if args.sell_on:
        sell_on = [s.strip() for s in args.sell_on.split(",") if s.strip()]
        unknown = [s for s in sell_on if s not in ("com", "in")]
        if unknown:
            sys.exit("unknown storefront(s): %s" % ", ".join(unknown))

    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    want = target(args.vertical, sell_on)

    db = connect()
    cur = db.cursor()
    rows = select_products(cur, args.category, ids)
    if not rows:
        sys.exit("no products matched")

    changing = [r for r in rows if needs_change(r, want)]
    unchanged = [r for r in rows if r not in changing]

    print("%d product(s) matched, %d to change"
          % (len(rows), len(changing)))
    for r in changing:
        print(describe(r, want))
    for r in unchanged:
        print("  %-6s %-10s already classified" % (r["product_id"],
                                                  r["product_code"]))

    if not changing:
        print("\nnothing to do")
        return

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = args.restore_file or "restore_vertical_%s.sql" % stamp
    restore_script(changing, path)
    print("\nundo script: %s" % path)

    reason = "vertical: %s, sites com=%s in=%s" % (
        want["product_vertical"], want["sell_on_com"], want["sell_on_in"])
    for r in changing:
        cur.execute(
            "UPDATE products SET product_vertical=%s, sell_on_com=%s,"
            " sell_on_in=%s WHERE product_id=%s",
            (want["product_vertical"], want["sell_on_com"],
             want["sell_on_in"], r["product_id"]))
        # The status is unchanged on purpose; the history row exists because a
        # product leaving a storefront is a catalogue event somebody will ask
        # about, and old_status = new_status says plainly that it was not a
        # lifecycle change.
        cur.execute(
            "INSERT INTO product_status_history (product_id, old_status,"
            " new_status, reason, changed_by, changed_at)"
            " VALUES (%s, %s, %s, %s, %s, NOW())",
            (r["product_id"], r["product_status"], r["product_status"],
             reason, args.by))
    db.commit()
    print("applied to %d product(s)" % len(changing))


if __name__ == "__main__":
    main()
