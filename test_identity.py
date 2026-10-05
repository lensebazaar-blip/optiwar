"""A TEST customer: a production account engineering signs in with to run the
signed-in assistant end to end.

Its purchases go through /test-checkout only, so each is an ``orders.is_test``
order: no money, no stock, no fulfilment, no revenue, no attribution. It is
sent no order notice. The assistant shows a TEST customer its own TEST orders;
every other account never sees a TEST order.
"""

ADDED_COLUMNS = (
    ("customers", (("is_test", "TINYINT(1) NOT NULL DEFAULT 0"),)),
)


def is_test_customer(cursor, customer_id):
    """True only for an account flagged ``customers.is_test``; False for a
    guest, an unknown id, or a database that does not have the column yet."""
    if not customer_id:
        return False
    try:
        cursor.execute("SELECT is_test FROM customers WHERE customer_id=%s",
                       (int(customer_id),))
        row = cursor.fetchone()
    except Exception:  # noqa: BLE001 - before the migration the flag is absent
        return False
    if not row:
        return False
    value = row.get("is_test") if isinstance(row, dict) else row[0]
    return bool(value)
