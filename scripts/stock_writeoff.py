#!/usr/bin/env python3
"""Write off physical stock that no longer exists (lost, damaged, stolen).

Zeroing product_quantity by hand is a silent edit: the storefront corrects
itself, and six months later nobody can say what happened to eleven frames.
This does the same job as an auditable, reversible transaction.

    # look, change nothing (default)
    python3 scripts/stock_writeoff.py --box 205 --reason "box lost in transit" --by sudhanshu

    # do it
    python3 scripts/stock_writeoff.py --box 205 --reason "box lost in transit" --by sudhanshu --apply

Per product, inside one transaction:

    product_quantity  -> 0
    product_status    -> OUT_OF_STOCK (or --status DISCONTINUED if it is never
                         coming back), only from ACTIVE/OUT_OF_STOCK; a manual
                         SEASONAL/DISCONTINUED/ARCHIVED decision is left alone
    sold_out_at       -> now, if not already set
    product_status_history += one row carrying the reason and the operator

Refuses to run when a live (non-archived, unfulfilled) order line still needs
one of these frames — that is a customer waiting for stock we do not have, and
it needs a decision, not a quantity edit. --force records the write-off anyway.

--apply first writes a restore script containing the exact prior quantities and
statuses, so the change can be undone without reconstructing them from memory.

Connection comes from MYSQL_HOST/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DB, the same
variables gunicorn runs with, so on the box:

    set -a; . /etc/optiwar.env; set +a   # or systemctl show -p Environment gunicorn
"""
import argparse
import datetime
import os
import sys

import pymysql

SELECT_COLS = ("product_id, product_code, product_name, product_quantity, "
               "product_status, product_cost, box_number, sold_out_at")

# Statuses this tool may move. A product someone deliberately marked SEASONAL,
# DISCONTINUED or ARCHIVED keeps that state: losing the stock does not change
# the commercial decision, and overwriting it would lose information.
MOVABLE = ("ACTIVE", "OUT_OF_STOCK")
TARGETS = ("OUT_OF_STOCK", "DISCONTINUED")


def connect():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "localhost"),
        user=os.environ.get("MYSQL_USER", ""),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DB", ""),
        cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def select_products(cur, box, ids):
    if box is not None:
        cur.execute("SELECT %s FROM products WHERE box_number=%%s "
                    "ORDER BY product_id" % SELECT_COLS, (str(box),))
    else:
        marks = ",".join(["%s"] * len(ids))
        cur.execute("SELECT %s FROM products WHERE product_id IN (%s) "
                    "ORDER BY product_id" % (SELECT_COLS, marks), tuple(ids))
    return cur.fetchall()


def unfulfilled_order_lines(cur, product_ids):
    """Unfulfilled, live, non-test lines for these frames, split by whether the
    customer actually paid.

    `fulfillment_status='pending'` is also the resting state of an abandoned
    cart — 383 such lines exist, the oldest from 2024 — so it cannot decide on
    its own whether anyone is owed a frame. A successful `payment_collector`
    row can: that is money taken for stock that no longer exists.
    """
    marks = ",".join(["%s"] * len(product_ids))
    cur.execute(
        "SELECT o.order_line_id, o.order_id, o.product_id, o.order_quantity,"
        " o.date_created, o.fulfillment_status, p.date_created AS paid_at"
        " FROM orders o"
        " LEFT JOIN payment_collector p"
        "   ON p.order_id = o.order_id AND p.status = 'TXN_SUCCESS'"
        " WHERE o.product_id IN (%s) AND COALESCE(o.archived,0)=0"
        "   AND COALESCE(o.is_test,0)=0"
        "   AND COALESCE(o.fulfillment_status,'') <> 'fulfilled'"
        " ORDER BY o.date_created" % marks, tuple(product_ids))
    lines = cur.fetchall()
    paid = [l for l in lines if l["paid_at"]]
    unpaid = [l for l in lines if not l["paid_at"]]
    return paid, unpaid


def needs_change(row, target):
    """A row already written off is left completely alone — re-running must not
    overwrite the reason a previous write-off (or a real sale) recorded."""
    if int(row["product_quantity"] or 0) > 0:
        return True
    return row["product_status"] in MOVABLE and row["product_status"] != target


def restore_script(rows, path):
    lines = ["-- Undo the write-off. Restores the exact prior quantity and status.",
             "-- Generated %s" % datetime.datetime.now().isoformat(timespec="seconds"),
             "START TRANSACTION;"]
    for r in rows:
        lines.append(
            "UPDATE products SET product_quantity=%s, product_status='%s', "
            "status_reason='restored write-off' WHERE product_id=%s;  -- %s"
            % (r["product_quantity"] if r["product_quantity"] is not None else 0,
               r["product_status"], r["product_id"], r["product_code"]))
    lines += ["COMMIT;", ""]
    with open(path, "w") as fh:
        fh.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--box", help="write off every product in this box_number")
    group.add_argument("--ids", help="comma-separated product_ids")
    ap.add_argument("--reason", required=True, help="why the stock is gone")
    ap.add_argument("--by", required=True, help="who authorised it")
    ap.add_argument("--status", default="OUT_OF_STOCK", choices=TARGETS,
                    help="OUT_OF_STOCK if it can be restocked (default), "
                         "DISCONTINUED if it is gone for good")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    ap.add_argument("--force", action="store_true",
                    help="proceed despite paid, unfulfilled order lines")
    ap.add_argument("--restore-file", default=None,
                    help="where to write the undo script (default: ./restore_<stamp>.sql)")
    args = ap.parse_args()

    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    db = connect()
    cur = db.cursor()
    rows = select_products(cur, args.box, ids)
    if not rows:
        print("nothing matched — no products selected")
        return 1

    units = sum(int(r["product_quantity"] or 0) for r in rows)
    cost = sum(int(r["product_quantity"] or 0) * float(r["product_cost"] or 0)
               for r in rows)

    print("%-6s %-10s %-28s %5s  %-14s" % ("id", "code", "name", "qty", "status"))
    for r in rows:
        print("%-6s %-10s %-28s %5s  %-14s"
              % (r["product_id"], r["product_code"], (r["product_name"] or "")[:28],
                 r["product_quantity"], r["product_status"]))
    print("\n%d products, %d units on hand, %.2f at cost -> %s"
          % (len(rows), units, cost, args.status))

    pending = [r for r in rows if needs_change(r, args.status)]
    if not pending:
        print("\nAlready written off — nothing to change.")
        return 0

    paid, unpaid = unfulfilled_order_lines(cur, [r["product_id"] for r in pending])
    if unpaid:
        print("\nUnpaid open lines (abandoned carts — not blocking):")
        for b in unpaid:
            print("  line %s  order %s  product %s  %s  %s"
                  % (b["order_line_id"], b["order_id"], b["product_id"],
                     b["date_created"], b["fulfillment_status"]))
    if paid:
        print("\nPAID, UNFULFILLED ORDER LINES for this stock:")
        for b in paid:
            print("  line %s  order %s  product %s  ordered %s  paid %s  %s"
                  % (b["order_line_id"], b["order_id"], b["product_id"],
                     b["date_created"], b["paid_at"], b["fulfillment_status"]))
        if not args.force:
            print("\nRefusing: a paying customer is owed stock that does not exist.\n"
                  "Refund, cancel or substitute those lines, or re-run with --force.")
            return 2
        print("\n--force given: recording the write-off anyway.")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = args.restore_file or os.path.join(os.getcwd(), "restore_%s.sql" % stamp)
    restore_script(pending, path)
    print("\nundo script: %s" % path)

    changed = 0
    try:
        for r in pending:
            pid, old_status = r["product_id"], r["product_status"]
            new_status = args.status if old_status in MOVABLE else old_status
            cur.execute(
                "UPDATE products SET product_quantity=0,"
                " product_status=%s, status_changed_at=NOW(), status_changed_by=%s,"
                " status_reason=%s, sold_out_at=COALESCE(sold_out_at, NOW())"
                " WHERE product_id=%s",
                (new_status, args.by, "write-off: %s" % args.reason, pid))
            # History is per state change, not per run: a second run over the same
            # box is a no-op rather than a second identical row.
            if new_status != old_status:
                cur.execute(
                    "INSERT INTO product_status_history"
                    " (product_id, old_status, new_status, reason, changed_by)"
                    " VALUES (%s,%s,%s,%s,%s)",
                    (pid, old_status, new_status,
                     "write-off: %s" % args.reason, args.by))
            elif int(r["product_quantity"] or 0) > 0:
                cur.execute(
                    "INSERT INTO product_status_history"
                    " (product_id, old_status, new_status, reason, changed_by)"
                    " VALUES (%s,%s,%s,%s,%s)",
                    (pid, old_status, new_status,
                     "write-off (quantity only): %s" % args.reason, args.by))
            changed += 1
        db.commit()
    except Exception:
        db.rollback()
        print("rolled back — nothing changed")
        raise

    print("applied to %d products; %d units written off (%.2f at cost)"
          % (changed, units, cost))
    for r in select_products(cur, args.box, ids):
        print("  %s %s -> qty %s, %s"
              % (r["product_id"], r["product_code"], r["product_quantity"],
                 r["product_status"]))
    print("\nStorefront and /api/products drop these within the 300s cache TTL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
