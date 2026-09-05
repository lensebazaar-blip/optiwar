#!/usr/bin/env python3
"""Razorpay payment reconciliation — Daily Report section.

What the morning report has to say about money: how many payments Razorpay
captured, which path recorded each one at Optiwar (browser, webhook, the
``payment.order_id -> order -> receipt`` recovery, the reconcile worker, or a
human), how many webhooks could not be matched, and whether any captured
payment sat Pending locally past the grace period.

The last one is the invariant, stated even when it holds:

    Razorpay captured/paid + Optiwar Pending > grace period  =  RED

Sourcing. Payments and their path come from the database: every settlement
writes ``payment_collector.payment_dump`` with ``source`` and ``resolved_by``
and an ``order_status`` row whose ``source`` names the path. Rejections and
suppressed duplicates never reach the database, so they are counted from the
``ACTIVITY:`` lines the application logs. The worker's own verdicts come from
the JSON it writes on every run; a stale file is a coverage gap, because a
worker that is not running is a safety net that is not there.
"""
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from report_db import SqlError, run_sql, to_int  # noqa: E402

WIDTH = 70
BANNER = "=" * WIDTH
WINDOW_HOURS = int(os.environ.get("ACR_REPORT_WINDOW_HOURS", "24"))
SINCE = "NOW() - INTERVAL %d HOUR" % WINDOW_HOURS

LOG_DIR = os.environ.get("OPTIWAR_LOG_DIR", "/var/log/optiwar")
DEBUG_LOG = os.path.join(LOG_DIR, "debug.log")
STATE_FILE = os.environ.get("RAZORPAY_RECONCILE_STATE",
                            os.path.join(LOG_DIR, "razorpay_reconcile_latest.json"))
# The worker runs every 10 minutes; a state file older than this means it is
# not running.
STATE_STALE_MINUTES = int(os.environ.get("RAZORPAY_RECONCILE_STALE_MINUTES", "60"))

GREEN, AMBER, RED = "GREEN", "AMBER", "RED"

# order_status.source -> report line. A settlement's source is written by the
# path that made it (see razorpay_settlement.settle and apply_paid_order).
SOURCE_LABELS = (
    ("storefront", "Matched by browser callback"),
    ("razorpay-webhook", "Matched by webhook"),
    ("razorpay-reconcile", "Recovered by reconciliation worker"),
    ("manual_reconcile", "Applied by manual reconciliation"),
)

# ACTIVITY tags counted from the application log.
LOG_TAGS = (
    ("RAZORPAY_WEBHOOK_UNMATCHED", "RAZORPAY_WEBHOOK_UNMATCHED"),
    ("RAZORPAY_ORDER_LOOKUP_RECOVERED", "Recovered by order_id -> receipt lookup"),
    ("RAZORPAY_AMOUNT_MISMATCH", "Amount mismatch"),
    ("RAZORPAY_CURRENCY_MISMATCH", "Currency mismatch"),
    ("RAZORPAY_DUPLICATE_SUPPRESSED", "Duplicate event suppressed"),
    ("RAZORPAY_WEBHOOK_REJECTED", "Webhook rejected (bad signature)"),
    ("PAYMENT_RECONCILIATION_EXCEPTION", "PAYMENT_RECONCILIATION_EXCEPTION"),
    ("PAYMENT_INVARIANT_RED", "Captured at Razorpay but Pending locally (> grace)"),
)

_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_TAG = re.compile(r"ACTIVITY:([A-Z_]+)")


def captured_by_source():
    """Razorpay payments recorded in the window, by the path that recorded them."""
    rows = run_sql(
        "SELECT COALESCE(s.source,'(unstated)'), COUNT(DISTINCT pc.payment_ref) "
        "FROM payment_collector pc "
        "LEFT JOIN order_status s ON s.order_id = pc.order_id "
        "  AND s.order_status_name = 'Processed' "
        "  AND s.note LIKE CONCAT('%%', pc.payment_ref) "
        "WHERE pc.status = 'TXN_SUCCESS' AND pc.payment_ref LIKE 'pay_%%' "
        "  AND pc.date_created >= %s GROUP BY 1" % SINCE)
    return {r[0]: to_int(r[1]) for r in rows}


def captured_pending_now():
    """Orders with a successful payment row but no Processed status — the
    pipeline half-applied. Must be 0."""
    rows = run_sql(
        "SELECT pc.order_id FROM payment_collector pc "
        "WHERE pc.status='TXN_SUCCESS' AND pc.date_created >= %s "
        "AND NOT EXISTS (SELECT 1 FROM order_status s WHERE s.order_id = pc.order_id "
        "                 AND s.order_status_name = 'Processed')" % SINCE)
    return [r[0] for r in rows]


def log_files(now=None):
    now = now or datetime.datetime.now()
    files = []
    yesterday = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    rotated = os.path.join(LOG_DIR, "debug.log.%s" % yesterday)
    if os.path.exists(rotated):
        files.append(rotated)
    if os.path.exists(DEBUG_LOG):
        files.append(DEBUG_LOG)
    return files


def tag_counts(files=None, now=None):
    """ACTIVITY tag -> count of lines within the window."""
    now = now or datetime.datetime.now()
    cutoff = now - datetime.timedelta(hours=WINDOW_HOURS)
    wanted = {tag for tag, _ in LOG_TAGS}
    counts = {tag: 0 for tag in wanted}
    for path in (files if files is not None else log_files(now)):
        try:
            with open(path, "r", errors="replace") as fh:
                for line in fh:
                    m = _TAG.search(line)
                    if not m or m.group(1) not in wanted:
                        continue
                    ts = _TS.match(line)
                    if ts:
                        try:
                            when = datetime.datetime.strptime(ts.group(1), "%Y-%m-%d %H:%M:%S")
                            if when < cutoff:
                                continue
                        except ValueError:
                            pass
                    counts[m.group(1)] += 1
        except OSError:
            continue
    return counts


def worker_state(path=None, now=None):
    """(summary dict or None, stale: bool, age_minutes or None)."""
    path = path or STATE_FILE
    now = now or datetime.datetime.now()
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, True, None
    try:
        gen = datetime.datetime.strptime(data.get("generated_at", ""), "%Y-%m-%d %H:%M:%S")
        age = (now - gen).total_seconds() / 60.0
    except ValueError:
        return data, True, None
    return data, age > STATE_STALE_MINUTES, age


def _collect():
    m, errs = {}, []

    def safe(key, fn):
        try:
            m[key] = fn()
        except SqlError as e:
            m[key] = None
            errs.append("%s: %s" % (key, e))

    safe("by_source", captured_by_source)
    safe("half_applied", captured_pending_now)
    m["tags"] = tag_counts()
    m["worker"], m["worker_stale"], m["worker_age"] = worker_state()
    return m, errs


_CACHE = []


def _collect_once():
    if not _CACHE:
        _CACHE.append(_collect())
    return _CACHE[0]


def _reset_cache():
    del _CACHE[:]


def status(m):
    tags = m.get("tags") or {}
    worker = m.get("worker") or {}
    if tags.get("PAYMENT_INVARIANT_RED") or (worker.get("over_grace") or 0):
        return RED, ("captured Razorpay payment(s) were Pending locally beyond the "
                     "grace period")
    if tags.get("PAYMENT_RECONCILIATION_EXCEPTION") or worker.get("exceptions"):
        return RED, "reconciliation exception: evidence conflicts, not auto-applied"
    if m.get("half_applied"):
        return RED, ("%d payment(s) recorded without a Processed status: %s"
                     % (len(m["half_applied"]), ", ".join(m["half_applied"][:5])))
    if tags.get("RAZORPAY_AMOUNT_MISMATCH") or tags.get("RAZORPAY_CURRENCY_MISMATCH"):
        return AMBER, "amount/currency mismatch refused"
    if tags.get("RAZORPAY_WEBHOOK_UNMATCHED"):
        return AMBER, "webhook(s) could not be matched to an order"
    if m.get("worker") is None or m.get("worker_stale"):
        return AMBER, "reconciliation worker state stale or missing — safety net unverified"
    if m.get("by_source") is None:
        return AMBER, "payment tables unreadable — coverage gap"
    return GREEN, None


def build():
    L = []
    add = L.append
    try:
        m, errs = _collect_once()
    except Exception as e:  # noqa: BLE001 - never break the daily report
        return "\n".join([BANNER, "  RAZORPAY PAYMENT RECONCILIATION", BANNER,
                          "  [WARN] section unavailable: %s" % e, BANNER])
    verdict, why = status(m)
    by_source = m.get("by_source")
    tags = m.get("tags") or {}
    worker = m.get("worker")

    add(BANNER)
    add("  RAZORPAY PAYMENT RECONCILIATION%sSTATUS: %s" % (" " * 15, verdict))
    add(BANNER)
    if why:
        add("  %s" % why)
    total = sum(by_source.values()) if by_source else None
    add("  Razorpay payments captured (last %dh)      %s"
        % (WINDOW_HOURS, total if total is not None else "n/a"))
    for key, label in SOURCE_LABELS:
        add("    %-42s %s" % (label, by_source.get(key, 0) if by_source is not None else "n/a"))
    if by_source:
        for key, n in sorted(by_source.items()):
            if key not in dict(SOURCE_LABELS):
                add("    %-42s %d" % ("source '%s'" % key, n))
    for tag, label in LOG_TAGS:
        if tag == "PAYMENT_INVARIANT_RED":
            continue
        add("    %-42s %d" % (label, tags.get(tag, 0)))
    add("")
    if worker is None:
        add("  Reconciliation worker: no state file — NOT RUNNING or never ran")
    else:
        add("  Reconciliation worker (last run %s%s): checked %s | settled %s | "
            "unpaid %s | duplicate %s | exception %s"
            % (worker.get("generated_at", "?"),
               ", STALE" if m.get("worker_stale") else "",
               worker.get("checked", "?"), worker.get("settled", "?"),
               worker.get("unpaid", "?"), worker.get("duplicate", "?"),
               worker.get("exception", "?")))
        for e in (worker.get("exceptions") or [])[:5]:
            add("      %s %s %s" % (e.get("order_id"), e.get("payment_id"), e.get("detail")))
    add("")
    over = tags.get("PAYMENT_INVARIANT_RED", 0) + ((worker or {}).get("over_grace") or 0)
    add("  INVARIANT  captured at Razorpay + Pending locally > grace: %d%s"
        % (over, "   [RED]" if over else ""))
    half = m.get("half_applied")
    add("  INVARIANT  payment recorded without Processed status: %s%s"
        % ("n/a" if half is None else len(half), "   [RED]" if half else ""))
    for e in errs[:6]:
        add("  [degraded] %s" % e)
    add("  Source: payment_collector/order_status, application ACTIVITY log, "
        "razorpay_reconcile state file. No card or customer data is read.")
    add("  Generated %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    add(BANNER)
    return "\n".join(L)


def findings():
    from reports.report_severity import ACTION, CRITICAL, Finding, WARNING
    try:
        m, errs = _collect_once()
    except Exception as e:  # noqa: BLE001
        return [Finding(ACTION, "payments", "payment section unavailable: %s" % e, "payments")]
    out = []
    verdict, why = status(m)
    if verdict == RED:
        out.append(Finding(CRITICAL, "payments", why, "payments"))
    elif verdict == AMBER:
        out.append(Finding(WARNING, "payments", why, "payments"))
    for e in errs[:6]:
        out.append(Finding(WARNING, "payments", "degraded metric %s" % e, "payments"))
    return out


def main():
    text = build()
    try:
        from reports.report_severity import emit
        emit("payments", findings())
    except Exception:  # noqa: BLE001
        pass
    print(text)


if __name__ == "__main__":
    main()
