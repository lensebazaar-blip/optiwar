#!/usr/bin/env python3
"""Create per-site invoice series and add invoice numbers to metadata."""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import get_connection, get_ow_connection


def _identifier(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
        raise ValueError(f"Invalid table name: {name!r}")
    return f"`{name}`"


def statements(series_table, meta_table):
    series = _identifier(series_table)
    meta = _identifier(meta_table)
    return [
        (
            f"CREATE TABLE IF NOT EXISTS {series} ("
            "fy CHAR(5) NOT NULL PRIMARY KEY, "
            "next_no INT UNSIGNED NOT NULL DEFAULT 1, "
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP "
            "ON UPDATE CURRENT_TIMESTAMP"
            ") ENGINE=InnoDB"
        ),
        f"ALTER TABLE {meta} ADD COLUMN invoice_no VARCHAR(16) NULL",
        f"ALTER TABLE {meta} ADD UNIQUE KEY uq_invoice_no (invoice_no)",
    ]


def _column_exists(cur, table_name, column_name):
    cur.execute(
        "SELECT 1 FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
        (table_name, column_name))
    return cur.fetchone() is not None


def _index_exists(cur, table_name, index_name):
    cur.execute(
        "SELECT COLUMN_NAME, NON_UNIQUE FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s "
        "ORDER BY SEQ_IN_INDEX",
        (table_name, index_name))
    rows = cur.fetchall()
    if not rows:
        return False
    if len(rows) != 1 or rows[0]["COLUMN_NAME"] != "invoice_no" \
            or rows[0]["NON_UNIQUE"]:
        raise RuntimeError(
            f"{index_name} on {table_name} exists but is not a unique "
            "single-column invoice_no index")
    return True


def migrate_schema(conn, series_table, meta_table):
    """Apply one site's migration; safe to repeat."""
    series = _identifier(series_table)
    meta = _identifier(meta_table)
    with conn.cursor() as cur:
        cur.execute(statements(series_table, meta_table)[0])
        if not _column_exists(cur, meta_table, "invoice_no"):
            cur.execute(
                f"ALTER TABLE {meta} ADD COLUMN invoice_no VARCHAR(16) NULL")
        if not _index_exists(cur, meta_table, "uq_invoice_no"):
            cur.execute(
                f"ALTER TABLE {meta} "
                "ADD UNIQUE KEY uq_invoice_no (invoice_no)")


def migrate(eu_connect=None, ow_connect=None):
    eu_conn = (eu_connect or get_connection)()
    try:
        migrate_schema(eu_conn, "eu_invoice_series", "eu_invoice_meta")
    finally:
        eu_conn.close()

    ow_conn = (ow_connect or get_ow_connection)()
    try:
        migrate_schema(ow_conn, "ops_invoice_series", "ops_invoice_meta")
    finally:
        ow_conn.close()


def dry_run():
    for schema, series_table, meta_table in (
            ("oslb6", "eu_invoice_series", "eu_invoice_meta"),
            ("optiwar2", "ops_invoice_series", "ops_invoice_meta")):
        print(f"-- {schema}")
        for sql in statements(series_table, meta_table):
            print(f"{sql};")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        dry_run()
    else:
        migrate()


if __name__ == "__main__":
    main()
