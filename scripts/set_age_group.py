#!/usr/bin/env python3
"""Assign the age range of a children's frame, for the Merchant feed.

The feed emits ``adult`` for an ordinary frame and nothing at all for a
``product_category_kids = 1`` one, because newborn / infant / toddler / kids is
an intended age range that a product name does not state. This is how a person
states it — and until they do, those products stay demoted rather than being
labelled adult to silence Google.

    # which children's frames are still unassigned
    python3 scripts/set_age_group.py --pending

    # look, change nothing (default)
    python3 scripts/set_age_group.py --codes AH98,AH99 --age infant --by sudhanshu

    # do it
    python3 scripts/set_age_group.py --codes AH98,AH99 --age infant --by sudhanshu --apply

Nothing else about the product is touched — not the status, not the quantity,
not the price, not the kids flag. ``--apply`` first writes a restore script
containing the exact prior values.

Connection comes from MYSQL_HOST/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DB, the same
variables gunicorn runs with, so on the box:

    set -a; . /etc/optiwar.env; set +a
"""
import argparse
import datetime
import os
import sys

import pymysql

# Google's five, and the empty string as a way to withdraw an assignment that
# turns out to be wrong.
AGES = ("newborn", "infant", "toddler", "kids", "adult")

COLS = ("product_id, product_code, product_name, product_status, "
        "product_category_kids, gmc_age_group")


def connect():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "localhost"),
        user=os.environ.get("MYSQL_USER", ""),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DB", ""),
        cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def pending(cur):
    """Children's frames with no assignment — the ones the feed is omitting."""
    cur.execute("SELECT %s FROM products WHERE product_category_kids = 1"
                " AND (gmc_age_group IS NULL OR gmc_age_group = '')"
                " ORDER BY product_code" % COLS)
    return cur.fetchall()


def by_codes(cur, codes):
    marks = ",".join(["%s"] * len(codes))
    cur.execute("SELECT %s FROM products WHERE product_code IN (%s)"
                " ORDER BY product_code" % (COLS, marks), tuple(codes))
    return cur.fetchall()


def restore_script(rows, path):
    lines = ["-- Undo the age assignment. Restores the exact prior values.",
             "-- Generated %s"
             % datetime.datetime.now().isoformat(timespec="seconds"),
             "START TRANSACTION;"]
    for r in rows:
        prior = r["gmc_age_group"]
        lines.append("UPDATE products SET gmc_age_group=%s WHERE product_id=%s;"
                     "  -- %s"
                     % ("NULL" if prior in (None, "") else "'%s'" % prior,
                        r["product_id"], r["product_code"]))
    lines += ["COMMIT;", ""]
    with open(path, "w") as fh:
        fh.write("\n".join(lines))


def show(rows):
    for r in rows:
        print("  %-6s %-10s %-30s kids=%s  %s"
              % (r["product_id"], r["product_code"],
                 (r["product_name"] or "")[:30],
                 r["product_category_kids"],
                 r["gmc_age_group"] or "(unassigned)"))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pending", action="store_true",
                    help="list children's frames with no assignment, and exit")
    ap.add_argument("--codes", help="comma-separated product_codes")
    ap.add_argument("--age", choices=AGES + ("",),
                    help="the age range, or '' to withdraw an assignment")
    ap.add_argument("--by", help="who authorised it")
    ap.add_argument("--apply", action="store_true",
                    help="write; otherwise dry run")
    ap.add_argument("--restore-file", default=None)
    args = ap.parse_args()

    db = connect()
    cur = db.cursor()

    if args.pending:
        rows = pending(cur)
        print("%d children's frame(s) awaiting an age assignment" % len(rows))
        show(rows)
        return

    if not args.codes or args.age is None or not args.by:
        sys.exit("--codes, --age and --by are required (or use --pending)")

    codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()]
    rows = by_codes(cur, codes)
    found = {r["product_code"].upper() for r in rows}
    missing = [c for c in codes if c not in found]
    if missing:
        sys.exit("no such product_code(s): %s" % ", ".join(missing))

    changing = [r for r in rows if (r["gmc_age_group"] or "") != args.age]
    print("%d product(s) matched, %d to change" % (len(rows), len(changing)))
    show(rows)
    # Assigning adult to a frame the catalogue calls a children's frame is
    # allowed but named: it contradicts product_category_kids, so the person
    # doing it should see that they are doing it.
    for r in changing:
        if args.age == "adult" and int(r["product_category_kids"] or 0):
            print("  NOTE %s is product_category_kids = 1 and is being called"
                  " adult" % r["product_code"])
    if not changing:
        print("\nnothing to do")
        return
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = args.restore_file or "restore_age_group_%s.sql" % stamp
    restore_script(changing, path)
    print("\nundo script: %s" % path)

    reason = "gmc_age_group: %s" % (args.age or "(withdrawn)")
    for r in changing:
        cur.execute("UPDATE products SET gmc_age_group=%s WHERE product_id=%s",
                    (args.age or None, r["product_id"]))
        # Same convention as the vertical classification: old_status =
        # new_status says plainly that this was not a lifecycle change, and the
        # row exists because a feed attribute somebody will ask about should be
        # explicable.
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
