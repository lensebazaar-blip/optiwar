#!/usr/bin/env python3
"""Backfill stock restoration for eligible full refunds.

Dry run is the default:

    python3 scripts/restock_refunded.py --by operator

Apply the changes:

    python3 scripts/restock_refunded.py --by operator --apply
"""
import argparse
import logging
import os
import sys

import pymysql

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from refunds import restock_unshipped


def connect():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "localhost"),
        user=os.environ.get("MYSQL_USER", ""),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DB", ""),
        cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def select_candidates(cursor, days, order_ids=None):
    sql = (
        "SELECT order_id, MAX(refund_id) AS refund_id FROM order_refunds "
        "WHERE refund_type='FULL' AND status IN ('PROCESSED','PENDING') "
        "AND requested_at >= NOW() - INTERVAL %s DAY")
    params = [days]
    if order_ids:
        sql += " AND order_id IN (%s)" % ','.join(['%s'] * len(order_ids))
        params.extend(order_ids)
    sql += " GROUP BY order_id ORDER BY order_id"
    cursor.execute(sql, tuple(params))
    return cursor.fetchall()


def run(db, args, logger=None):
    cursor = db.cursor()
    candidates = select_candidates(cursor, args.days, args.order)
    print("%d full-refund order(s) in scope" % len(candidates))
    for candidate in candidates:
        try:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM stock_restocks WHERE order_id=%s",
                (candidate['order_id'],))
            had_restock = cursor.fetchone()['n'] > 0
            restored = restock_unshipped(
                cursor, candidate['order_id'], candidate['refund_id'], args.by,
                reason='backfill', logger=logger)
            detail = ', '.join(
                "%s +%s" % (line['product_code'] or line['product_id'],
                            line['qty'])
                for line in restored)
            if args.apply:
                db.commit()
                print("APPLIED order:%s %s" % (
                    candidate['order_id'],
                    detail or ("already restored" if had_restock
                               else "no eligible lines")))
            else:
                db.rollback()
                print("DRY RUN order:%s %s" % (
                    candidate['order_id'],
                    "would restore " + detail if detail
                    else ("already restored" if had_restock
                          else "no eligible lines")))
        except Exception:
            db.rollback()
            raise
    if not candidates:
        db.rollback()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="commit each order; default is dry run")
    parser.add_argument("--by", required=True, help="operator running the backfill")
    parser.add_argument("--days", type=int, default=90,
                        help="include refunds from the last N days (default: 90)")
    parser.add_argument("--order", action="append", default=[],
                        help="limit to this order ID; may be repeated")
    args = parser.parse_args(argv)
    if args.days < 0:
        parser.error("--days must be zero or greater")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db = connect()
    try:
        return run(db, args, logging.getLogger("restock_refunded"))
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
