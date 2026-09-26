#!/usr/bin/env python3
"""Returned parcels & reshipments (optiwar.in) — Daily Report section.

What Ops and the owner need to know each morning about parcels the courier
brought back: how many are physically held, how many still owe the reship
fee, how many are paid and waiting to leave, how many are inside the final
window before abandonment, and what closed yesterday either way.

Every deadline printed here is the one stored on the row
(``order_reshipments.abandon_at``, set at physical receipt); this section
computes nothing of its own. A metric the database will not answer is
printed as ``n/a`` — never as 0.

Alerts (findings) this section raises:

    RETURNED_TO_OPS with no customer notification after 5 minutes   WARNING
    RESHIP_PAID but not shipped within the operating threshold       WARNING
    parcel inside the final window before abandonment                WARNING
    ABANDONED but the Ops platform has not been told                 ACTION

    RESHIP_SHIP_THRESHOLD_HOURS   how long a PAID parcel may wait (default 48)
    RESHIP_FINAL_WINDOW_DAYS      the final window (default 5, same as the app)
    RESHIP_NOTIFY_GRACE_MINUTES   grace before a missing notice alerts (default 5)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from report_db import SqlError, run_sql, to_int  # noqa: E402

WIDTH = 70
BANNER = "=" * WIDTH
GREEN, AMBER, RED = "GREEN", "AMBER", "RED"

SHIP_THRESHOLD_HOURS = int(os.environ.get("RESHIP_SHIP_THRESHOLD_HOURS", "48"))
FINAL_WINDOW_DAYS = int(os.environ.get("RESHIP_FINAL_WINDOW_DAYS", "5"))
NOTIFY_GRACE_MINUTES = int(os.environ.get("RESHIP_NOTIFY_GRACE_MINUTES", "5"))

OPEN = "('RETURNED','PAYMENT_PENDING')"
NA = "n/a"

METRICS = (
    ("held", "Returned parcels held (unpaid, paid, awaiting reship)",
     "SELECT COUNT(*) FROM order_reshipments WHERE status IN "
     "('RETURNED','PAYMENT_PENDING','PAID')"),
    ("awaiting_payment", "Awaiting Rs 250 payment",
     "SELECT COUNT(*) FROM order_reshipments WHERE status IN %s "
     "AND hold_reason IS NULL" % OPEN),
    ("on_hold", "On administrative hold (countdown paused)",
     "SELECT COUNT(*) FROM order_reshipments WHERE status IN %s "
     "AND hold_reason IS NOT NULL" % OPEN),
    ("paid_awaiting_reship", "Paid - awaiting reship",
     "SELECT COUNT(*) FROM order_reshipments WHERE status='PAID'"),
    ("approaching", "Approaching abandonment (<= %d days)" % FINAL_WINDOW_DAYS,
     "SELECT COUNT(*) FROM order_reshipments WHERE status IN %s "
     "AND hold_reason IS NULL AND abandon_at IS NOT NULL "
     "AND abandon_at <= NOW() + INTERVAL %d DAY" % (OPEN, FINAL_WINDOW_DAYS)),
    ("abandoned_24h", "Abandoned (last 24h)",
     "SELECT COUNT(*) FROM order_reshipments WHERE status='ABANDONED' "
     "AND abandoned_at >= NOW() - INTERVAL 24 HOUR"),
    ("reshipped_24h", "Reshipped (last 24h)",
     "SELECT COUNT(*) FROM order_reshipments WHERE status='RESHIPPED' "
     "AND reshipped_at >= NOW() - INTERVAL 24 HOUR"),
)

ALERTS = (
    ("unnotified", "RETURNED_TO_OPS but the customer was not told",
     "SELECT r.order_id, r.ops_return_confirmed_at FROM order_reshipments r "
     "WHERE r.status IN %s AND r.ops_return_confirmed_at < NOW() - INTERVAL %d MINUTE "
     "AND NOT EXISTS (SELECT 1 FROM reship_events e WHERE e.reship_uuid=r.reship_uuid "
     "AND e.event_type='reship.notified' AND e.payload LIKE '%%reship.available%%' "
     # a claim whose delivery then failed on that channel is not a notice
     "AND NOT EXISTS (SELECT 1 FROM reship_events f WHERE f.reship_uuid=e.reship_uuid "
     "AND f.event_type='reship.notify_failed' AND f.payload LIKE '%%reship.available%%' "
     "AND JSON_UNQUOTE(JSON_EXTRACT(f.payload,'$.channel'))"
     "=JSON_UNQUOTE(JSON_EXTRACT(e.payload,'$.channel'))))"
     % (OPEN, NOTIFY_GRACE_MINUTES)),
    ("paid_unshipped", "RESHIP_PAID for more than %dh and not shipped" % SHIP_THRESHOLD_HOURS,
     "SELECT order_id, paid_at FROM order_reshipments WHERE status='PAID' "
     "AND paid_at < NOW() - INTERVAL %d HOUR" % SHIP_THRESHOLD_HOURS),
    ("final_window", "Inside the final %d days before abandonment" % FINAL_WINDOW_DAYS,
     "SELECT order_id, abandon_at FROM order_reshipments WHERE status IN %s "
     "AND hold_reason IS NULL AND abandon_at IS NOT NULL "
     "AND abandon_at <= NOW() + INTERVAL %d DAY ORDER BY abandon_at"
     % (OPEN, FINAL_WINDOW_DAYS)),
    ("unsynced", "ABANDONED but the Ops platform has not acknowledged it",
     "SELECT order_id, abandoned_at FROM order_reshipments WHERE status='ABANDONED' "
     "AND (ops_sync_status IS NULL OR ops_sync_status IN ('PENDING','FAILED'))"),
)


def collect(sql=run_sql):
    """(metrics, alerts, errors). A metric or alert whose query failed is
    ``None`` and its error recorded, so the section prints n/a there."""
    metrics, alerts, errors = {}, {}, []
    for key, _label, query in METRICS:
        try:
            rows = sql(query)
            metrics[key] = to_int(rows[0][0]) if rows and rows[0] else 0
        except SqlError as exc:
            metrics[key] = None
            errors.append("%s: %s" % (key, exc))
    for key, _label, query in ALERTS:
        try:
            alerts[key] = list(sql(query))
        except SqlError as exc:
            alerts[key] = None
            errors.append("%s: %s" % (key, exc))
    return metrics, alerts, errors


def status_of(metrics, alerts):
    if alerts.get("unsynced"):
        return RED
    if any(alerts.get(k) for k in ("unnotified", "paid_unshipped", "final_window")):
        return AMBER
    if any(v is None for v in metrics.values()) or any(v is None for v in alerts.values()):
        return AMBER
    return GREEN


def build(metrics=None, alerts=None, errors=None):
    if metrics is None:
        metrics, alerts, errors = collect()
    L = []
    add = L.append
    add(BANNER)
    add("  RETURNED PARCELS & RESHIPMENTS (optiwar.in)   STATUS: %s"
        % status_of(metrics, alerts))
    add(BANNER)
    add("  Holding period runs from Ops' physical receipt (stored deadline per row).")
    add("")
    for key, label, _q in METRICS:
        v = metrics.get(key)
        add("  %-52s %s" % (label, NA if v is None else v))
    for key, label, _q in ALERTS:
        rows = alerts.get(key)
        if rows is None:
            add("")
            add("  [n/a] %s: not readable" % label)
            continue
        if not rows:
            continue
        add("")
        add("  [ALERT] %s (%d)" % (label, len(rows)))
        for r in rows[:10]:
            add("    - %s  (%s)" % (r[0], r[1] if len(r) > 1 else ""))
        if len(rows) > 10:
            add("    ... and %d more" % (len(rows) - 10))
    if errors:
        add("")
        for e in errors:
            add("  n/a: %s" % e)
    add("  Abandoned = closed for online reshipment; disposal needs its own authorisation.")
    add(BANNER)
    return "\n".join(L)


def findings(metrics=None, alerts=None, errors=None):
    from reports.report_severity import ACTION, Finding, WARNING
    if metrics is None:
        metrics, alerts, errors = collect()
    out = []
    for e in errors or ():
        out.append(Finding(WARNING, "reship", "reship report: %s" % e, "reship"))
    sev = {"unnotified": WARNING, "paid_unshipped": WARNING, "final_window": WARNING,
           "unsynced": ACTION}
    for key, label, _q in ALERTS:
        rows = alerts.get(key)
        if not rows:
            continue
        for r in rows:
            out.append(Finding(sev[key], "reship", "%s: %s (%s)"
                               % (label, r[0], r[1] if len(r) > 1 else ""), "reship"))
    return out


def main():
    metrics, alerts, errors = collect()
    print(build(metrics, alerts, errors))
    try:
        from reports.report_severity import emit
        emit("reship", findings(metrics, alerts, errors))
    except Exception:  # noqa: BLE001 - the section still stands on its own
        pass


if __name__ == "__main__":
    main()
