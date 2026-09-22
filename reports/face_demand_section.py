#!/usr/bin/env python3
"""Face Demand Intelligence — Daily Report section.

Frame-fit analytics: which spectacle frames customers with a face scan looked
at, whether the frame fitted, which sizes are demanded, and where stock lags
demand. Every figure is restricted to lines a face can be fitted to — the
same rule as ``face_cart.is_frame_line``: the product's category is exactly
"Spectacles Frame" and it is not a contact lens. A contact lens (vertical
CONTACT_LENS, category NULL), a hearing aid or an uncategorised product never
enters these numbers, however it reached ``face_demand_log`` historically.

Rows are excluded at query time; nothing is deleted from the log.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from report_db import SqlError, run_sql, to_int  # noqa: E402

WIDTH = 70
BANNER = "=" * WIDTH
FRAME_CATEGORY = "Spectacles Frame"
LENS_VERTICAL = "CONTACT_LENS"

# The one place the frame rule is stated in SQL. Every query below joins the
# demand log to the product and applies exactly this predicate.
FRAME_JOIN = ("JOIN products p ON p.product_id = d.product_id "
              "AND TRIM(COALESCE(p.product_category,'')) = '%s' "
              "AND COALESCE(p.product_vertical,'') <> '%s'"
              % (FRAME_CATEGORY, LENS_VERTICAL))

FRAME_DEMAND = "FROM face_demand_log d " + FRAME_JOIN


def _label(status):
    return "Matched" if status == "matched" else "Not Matched"


def _collect():
    m, errs = {}, []

    def safe(key, fn):
        try:
            m[key] = fn()
        except SqlError as e:
            m[key] = None
            errs.append("%s: %s" % (key, e))

    safe("scans", lambda: run_sql(
        "SELECT COUNT(*), COUNT(DISTINCT customer_id) FROM face_measurements"))

    safe("views_24h", lambda: run_sql(
        "SELECT d.match_status, COUNT(*), COUNT(DISTINCT d.customer_id) "
        "%s WHERE d.created_at >= NOW() - INTERVAL 24 HOUR GROUP BY d.match_status"
        % FRAME_DEMAND))
    safe("views_all", lambda: run_sql(
        "SELECT d.match_status, COUNT(*), COUNT(DISTINCT d.customer_id) "
        "%s GROUP BY d.match_status" % FRAME_DEMAND))

    safe("sizes", lambda: run_sql(
        "SELECT d.recommended_size, COUNT(DISTINCT d.customer_id) c, COUNT(*) "
        "%s WHERE d.recommended_size <> '' GROUP BY d.recommended_size "
        "ORDER BY c DESC LIMIT 10" % FRAME_DEMAND))

    safe("gap", lambda: run_sql(
        "SELECT x.recommended_size, x.customers, IFNULL(s.cnt, 0) FROM ("
        "SELECT d.recommended_size, COUNT(DISTINCT d.customer_id) customers "
        "%s WHERE d.recommended_size <> '' GROUP BY d.recommended_size) x "
        "LEFT JOIN (SELECT product_size, COUNT(*) cnt FROM products "
        "WHERE product_quantity > 0 AND TRIM(COALESCE(product_category,'')) = '%s' "
        "GROUP BY product_size) s ON s.product_size = x.recommended_size "
        "ORDER BY x.customers DESC LIMIT 10" % (FRAME_DEMAND, FRAME_CATEGORY)))

    safe("nomatch", lambda: run_sql(
        "SELECT d.product_code, d.product_size, COUNT(*) v, COUNT(DISTINCT d.customer_id) "
        "%s WHERE d.match_status = 'not_matched' "
        "GROUP BY d.product_code, d.product_size ORDER BY v DESC LIMIT 10" % FRAME_DEMAND))

    # Rows the frame rule excludes, so the exclusion is visible, not silent.
    safe("excluded", lambda: run_sql(
        "SELECT COUNT(*), COUNT(DISTINCT d.product_code) FROM face_demand_log d "
        "LEFT JOIN products p ON p.product_id = d.product_id "
        "WHERE p.product_id IS NULL "
        "OR TRIM(COALESCE(p.product_category,'')) <> '%s' "
        "OR COALESCE(p.product_vertical,'') = '%s'" % (FRAME_CATEGORY, LENS_VERTICAL)))
    return m, errs


def build():
    L = []
    add = L.append
    add(BANNER)
    add("  SECTION 3: FACE DEMAND INTELLIGENCE  (spectacle frames only)")
    add(BANNER)
    try:
        m, errs = _collect()
    except Exception as e:  # noqa: BLE001 - never break the daily report
        add("  [WARN] section unavailable: %s" % e)
        add(BANNER)
        return "\n".join(L)

    scans = m.get("scans")
    if scans:
        add("")
        add("  Total face scans: %s  |  Unique customers: %s"
            % (to_int(scans[0][0]), to_int(scans[0][1])))

    views_24h = m.get("views_24h")
    if views_24h:
        add("")
        add("  FRAME PAGE VIEWS (Last 24h, logged-in users with face data):")
        for status, views, customers in views_24h:
            add("    %-14s  Views: %-6s  Customers: %s" % (_label(status), views, customers))
    elif views_24h is not None:
        add("")
        add("  No frame face-match views logged in last 24h.")

    views_all = m.get("views_all")
    if views_all:
        add("")
        add("  FRAME PAGE VIEWS (All Time):")
        for status, views, customers in views_all:
            add("    %-14s  Views: %-6s  Customers: %s" % (_label(status), views, customers))

    sizes = m.get("sizes")
    if sizes:
        add("")
        add("  MOST DEMANDED FRAME SIZES (by unique customers):")
        add("    %-16s %10s %8s" % ("Size", "Customers", "Views"))
        add("    %s %s %s" % ("-" * 16, "-" * 10, "-" * 8))
        for size, cust, views in sizes:
            add("    %-16s %10s %8s" % (size, cust, views))

    gap = m.get("gap")
    if gap:
        add("")
        add("  STOCKING GAP ANALYSIS (frame demand vs frame inventory):")
        add("    %-16s %8s %10s %12s" % ("Size", "Demand", "In Stock", "Status"))
        add("    %s %s %s %s" % ("-" * 16, "-" * 8, "-" * 10, "-" * 12))
        for size, demand, stock in gap:
            stock_n, demand_n = to_int(stock), to_int(demand)
            status = ("*** NO STOCK" if stock_n == 0
                      else "LOW STOCK" if stock_n < demand_n else "OK")
            add("    %-16s %8s %10s %12s" % (size, demand, stock, status))

    nomatch = m.get("nomatch")
    if nomatch:
        add("")
        add("  TOP NOT-MATCHED FRAMES (frames customers viewed but didn't fit):")
        add("    %-12s %-14s %6s %10s" % ("Code", "Size", "Views", "Customers"))
        add("    %s %s %s %s" % ("-" * 12, "-" * 14, "-" * 6, "-" * 10))
        for code, size, views, cust in nomatch:
            add("    %-12s %-14s %6s %10s" % (code, size, views, cust))

    excluded = m.get("excluded")
    if excluded:
        n, codes = to_int(excluded[0][0]), to_int(excluded[0][1])
        add("")
        add("  Excluded from frame analytics: %d log row(s) across %d non-frame "
            "product(s) (contact lenses, hearing aids, uncategorised). Kept in "
            "face_demand_log; not counted above." % (n, codes))

    for e in errs[:6]:
        add("  [degraded] %s" % e)
    add("")
    return "\n".join(L)


if __name__ == "__main__":
    print(build())
