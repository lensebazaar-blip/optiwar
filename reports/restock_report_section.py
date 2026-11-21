#!/usr/bin/env python3
"""Daily Report section for processed full refunds awaiting stock restock."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from report_db import SqlError, run_sql, to_int  # noqa: E402

WIDTH = 70
BANNER = "=" * WIDTH
GREEN, AMBER = "GREEN", "AMBER"

QUERY = """SELECT r.order_id, p.product_code, o.order_quantity,
       DATE(MIN(COALESCE(r.completed_at, r.requested_at))) AS refunded_on,
       DATEDIFF(CURDATE(), DATE(MIN(COALESCE(r.completed_at, r.requested_at)))) AS days_open
FROM order_refunds r
JOIN orders o ON o.order_id = r.order_id
JOIN products p ON p.product_id = o.product_id
WHERE r.status = 'PROCESSED' AND r.refund_type = 'FULL'
  AND o.fulfillment_status = 'fulfilled' AND o.order_quantity > 0
  AND COALESCE(p.product_vertical, '') <> 'SMART_EYEWEAR'
  AND NOT EXISTS (SELECT 1 FROM orders t WHERE t.order_id = r.order_id AND t.is_test = 1)
  AND NOT EXISTS (SELECT 1 FROM order_status s WHERE s.order_id = r.order_id
                  AND s.order_status_name IN ('Shipped','Complete','Delivery-assist','Returned','Processed-Reverse','Shipped-Reverse'))
  AND NOT EXISTS (SELECT 1 FROM ops_shipping_awb a WHERE a.ow_order_id = r.order_id)
  AND NOT EXISTS (SELECT 1 FROM stock_restocks k WHERE k.order_line_id = o.order_line_id)
GROUP BY o.order_line_id, r.order_id, p.product_code, o.order_quantity
ORDER BY refunded_on, o.order_line_id"""


def collect(sql=run_sql):
    try:
        return list(sql(QUERY)), None
    except SqlError as exc:
        return [], str(exc) or "unknown SQL error"


def _line(row):
    order_id, product_code, qty, refunded_on, days_open = row
    return ("OW-%s" % order_id, product_code, to_int(qty), refunded_on,
            to_int(days_open))


def build(rows=None, error=None):
    if rows is None and error is None:
        rows, error = collect()
    rows = rows or []
    status = AMBER if error or rows else GREEN
    L = []
    add = L.append
    add(BANNER)
    add("  REFUNDS AWAITING RESTOCK (unshipped full refunds)   STATUS: %s" % status)
    add(BANNER)
    if error:
        add("  [n/a] Refunds awaiting restock: not readable (%s)" % error)
    elif rows:
        add("  Lines not yet restocked: %d" % len(rows))
        for row in rows:
            order_id, product_code, qty, refunded_on, days_open = _line(row)
            add("    - %s  %s x%s  refunded %s  open %sd"
                % (order_id, product_code, qty, refunded_on, days_open))
        add("  Restock with: scripts/restock_refunded.py --by NAME --order <id> "
            "--apply (dry run first without --apply)")
    else:
        add("  None - every unshipped full refund has its stock back.")
    add(BANNER)
    return "\n".join(L)


def findings(rows=None, error=None):
    from reports.report_severity import Finding, WARNING
    if rows is None and error is None:
        rows, error = collect()
    if error:
        return [Finding(WARNING, "restock", "restock report: %s" % error,
                        "restock")]
    out = []
    for row in rows or ():
        order_id, product_code, qty, refunded_on, days_open = _line(row)
        out.append(Finding(
            WARNING, "restock",
            "Refund awaiting restock: %s %s x%s (refunded %s, open %sd)"
            % (order_id, product_code, qty, refunded_on, days_open),
            "restock"))
    return out


def main():
    rows, error = collect()
    print(build(rows, error))
    try:
        from reports.report_severity import emit
        emit("restock", findings(rows, error))
    except Exception:  # noqa: BLE001 - the section still stands on its own
        pass


if __name__ == "__main__":
    main()
