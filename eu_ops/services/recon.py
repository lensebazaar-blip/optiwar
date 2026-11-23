"""Read-only view of the reconciliation ledger on the WMS box (.198).

The ledger is written only by the recon scripts. This module never writes: the
dashboard is here to show what the evidence says, not to become a second way of
asserting that money arrived. Its database user has SELECT and nothing else, so
a bug here cannot change a settlement.
"""
import decimal
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

import config

ZERO = decimal.Decimal("0")

# a receivable's state, in the order the money actually moves
STATE_ORDER = ["OPEN", "DELIVERED", "IN_PAYOUT", "BANK_CREDITED", "SETTLED",
               "RTO", "REFUNDED", "WRITTEN_OFF"]
CLOSED_STATES = ("SETTLED", "RTO", "REFUNDED", "WRITTEN_OFF")


def get_connection():
    return pymysql.connect(
        host=config.RECON_DB_HOST, port=config.RECON_DB_PORT,
        user=config.RECON_DB_USER, password=config.RECON_DB_PASS,
        database=config.RECON_DB_NAME, charset="utf8mb4",
        cursorclass=DictCursor, autocommit=True,
        connect_timeout=8, read_timeout=30)


@contextmanager
def cursor():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def day(date_text):
    """Everything the dashboard shows for one shipping date."""
    with cursor() as cur:
        cur.execute(
            """SELECT r.*,
                      GROUP_CONCAT(DISTINCT b.id) AS batch_ids,
                      GROUP_CONCAT(DISTINCT b.batch_ref
                                   ORDER BY b.batch_ref SEPARATOR ', ') AS batches,
                      GROUP_CONCAT(DISTINCT b.source) AS batch_sources,
                      MAX(t.txn_date) AS banked_on,
                      MAX(t.bank_ref) AS bank_ref,
                      MAX(a.account_tail) AS bank_tail
                 FROM recon_receivable r
                 LEFT JOIN recon_match m ON m.receivable_id = r.id
                       AND m.is_active = 1 AND m.match_level = 'order_to_line'
                 LEFT JOIN recon_settlement_line l ON l.id = m.settlement_line_id
                 LEFT JOIN recon_payout_batch b ON b.id = l.batch_id
                 LEFT JOIN recon_bank_txn t ON t.id = b.bank_txn_id
                 LEFT JOIN recon_bank_account a ON a.id = t.bank_account_id
                WHERE DATE(r.shipped_at) = %s
                GROUP BY r.id
                ORDER BY r.payment_bucket, r.order_id""", (date_text,))
        rows = cur.fetchall()

        cur.execute(
            """SELECT e.*, r.order_id AS rec_order
                 FROM recon_exception e
                 LEFT JOIN recon_receivable r ON r.id = e.receivable_id
                WHERE e.status = 'OPEN' AND DATE(r.shipped_at) = %s
                ORDER BY FIELD(e.severity,'HIGH','MEDIUM','LOW'), e.id""",
            (date_text,))
        exceptions = cur.fetchall()

        cur.execute(
            """SELECT source_site, run_kind, status, rows_ingested, data_as_of,
                      watermark_order, error_text, started_at, finished_at
                 FROM recon_source_run
                WHERE DATE(data_as_of) = %s OR DATE(started_at) = %s
                ORDER BY source_site, id DESC""", (date_text, date_text))
        runs = cur.fetchall()

    due = sum((r["amount_due_inr"] for r in rows), ZERO)
    settled = sum((r["amount_settled_inr"] for r in rows), ZERO)
    rto = sum((r["amount_due_inr"] for r in rows if r["state"] == "RTO"), ZERO)
    open_money = sum((r["amount_due_inr"] - r["amount_settled_inr"]
                      for r in rows if r["state"] not in CLOSED_STATES), ZERO)
    buckets = {}
    for r in rows:
        b = buckets.setdefault(r["payment_bucket"] or "?",
                               {"n": 0, "due": ZERO, "settled": ZERO})
        b["n"] += 1
        b["due"] += r["amount_due_inr"]
        b["settled"] += r["amount_settled_inr"]

    return {
        "date": date_text,
        "rows": rows,
        "exceptions": exceptions,
        "runs": runs,
        "totals": {"orders": len(rows), "due": due, "settled": settled,
                   "rto": rto, "open": open_money,
                   "closed": sum(1 for r in rows if r["state"] in CLOSED_STATES)},
        "buckets": sorted(buckets.items()),
    }


def receivable(receivable_id):
    """One order: what it is, what proves it, and every state it passed."""
    with cursor() as cur:
        cur.execute("SELECT * FROM recon_receivable WHERE id=%s",
                    (receivable_id,))
        rec = cur.fetchone()
        if not rec:
            return None

        cur.execute(
            """SELECT * FROM recon_state_history
                WHERE receivable_id=%s ORDER BY id""", (receivable_id,))
        history = cur.fetchall()

        cur.execute(
            """SELECT m.*, l.order_id AS line_order, l.awb AS line_awb,
                      l.gross_amount, l.fee_amount, l.tax_amount, l.net_amount,
                      l.entity_kind, l.collected_at, l.payment_ref,
                      l.raw_reference, b.id AS batch_id, b.batch_ref, b.source,
                      b.payout_date, b.bank_ref, t.txn_date, t.credit_amount,
                      t.narration, a.account_tail
                 FROM recon_match m
                 JOIN recon_settlement_line l ON l.id = m.settlement_line_id
                 JOIN recon_payout_batch b ON b.id = l.batch_id
                 LEFT JOIN recon_bank_txn t ON t.id = b.bank_txn_id
                 LEFT JOIN recon_bank_account a ON a.id = t.bank_account_id
                WHERE m.receivable_id=%s AND m.is_active=1
                  AND m.match_level='order_to_line'
                ORDER BY m.id""", (receivable_id,))
        matches = cur.fetchall()

        cur.execute(
            """SELECT c.*, l.gross_amount, b.batch_ref, t.txn_date
                 FROM recon_match_candidate c
                 LEFT JOIN recon_settlement_line l ON l.id = c.settlement_line_id
                 LEFT JOIN recon_payout_batch b ON b.id = COALESCE(c.batch_id,
                                                                   l.batch_id)
                 LEFT JOIN recon_bank_txn t ON t.id = c.bank_txn_id
                WHERE c.receivable_id=%s AND c.status='PENDING'
                ORDER BY c.score DESC""", (receivable_id,))
        candidates = cur.fetchall()

        cur.execute(
            """SELECT * FROM recon_exception
                WHERE receivable_id=%s ORDER BY id DESC""", (receivable_id,))
        exceptions = cur.fetchall()

    return {"rec": rec, "history": history, "matches": matches,
            "candidates": candidates, "exceptions": exceptions}


def payout(batch_id):
    """One payout batch: its lines, its bank credit, and what it is short of."""
    with cursor() as cur:
        cur.execute(
            """SELECT b.*, a.account_tail AS expected_tail, t.txn_date,
                      t.credit_amount, t.narration, t.bank_ref AS credited_ref,
                      ta.account_tail AS credited_tail, f.original_name,
                      f.sha256, f.uploaded_at
                 FROM recon_payout_batch b
                 LEFT JOIN recon_bank_account a ON a.id = b.expected_account_id
                 LEFT JOIN recon_bank_txn t ON t.id = b.bank_txn_id
                 LEFT JOIN recon_bank_account ta ON ta.id = t.bank_account_id
                 LEFT JOIN recon_import_file f ON f.id = b.import_file_id
                WHERE b.id=%s""", (batch_id,))
        batch = cur.fetchone()
        if not batch:
            return None

        cur.execute(
            """SELECT l.*, r.id AS receivable_id, r.state AS rec_state,
                      r.amount_due_inr
                 FROM recon_settlement_line l
                 LEFT JOIN recon_match m ON m.settlement_line_id = l.id
                       AND m.is_active = 1 AND m.match_level = 'order_to_line'
                 LEFT JOIN recon_receivable r ON r.id = m.receivable_id
                WHERE l.batch_id=%s
                ORDER BY l.entity_kind DESC, l.order_id""", (batch_id,))
        lines = cur.fetchall()

    return {"batch": batch, "lines": lines}


def run_day(date_text, mode, actor):
    """Ask the runner on the ledger box to dry-run or commit one shipping date.

    This module decides nothing about money. It carries the request to the box
    that owns the ledger and brings the output back, so an operator reads what
    a day would do before anything is written.
    """
    import requests

    url = "http://%s:%s/run" % (config.RECON_RUNNER_HOST,
                                config.RECON_RUNNER_PORT)
    try:
        resp = requests.post(
            url, timeout=(5, 900),
            headers={"X-Recon-Token": config.RECON_RUNNER_TOKEN},
            json={"date": date_text, "mode": mode, "actor": actor})
    except requests.RequestException as exc:
        return {"ok": False, "date": date_text, "mode": mode, "steps": [],
                "error": "the reconciliation runner did not answer: %s" % exc}

    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "date": date_text, "mode": mode, "steps": [],
                "error": "runner returned HTTP %s" % resp.status_code}
    if resp.status_code != 200:
        data["ok"] = False
    return data


def runner_health():
    """(up, dates already dry run) so the Commit button knows when to unlock."""
    import requests
    try:
        resp = requests.get("http://%s:%s/health" % (config.RECON_RUNNER_HOST,
                                                     config.RECON_RUNNER_PORT),
                            timeout=4)
        data = resp.json()
        return bool(data.get("ok")), data.get("dry_run_seen", [])
    except Exception:
        return False, []


def recent_days(limit=30):
    """Dates that have been ingested, newest first, with a one-line result."""
    with cursor() as cur:
        cur.execute(
            """SELECT DATE(shipped_at) AS d, COUNT(*) AS orders,
                      SUM(amount_due_inr) AS due,
                      SUM(amount_settled_inr) AS settled,
                      SUM(state IN ('SETTLED','RTO','REFUNDED','WRITTEN_OFF'))
                        AS closed
                 FROM recon_receivable
                GROUP BY DATE(shipped_at)
                ORDER BY d DESC
                LIMIT %s""", (limit,))
        return cur.fetchall()
