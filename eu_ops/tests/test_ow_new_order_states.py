import sys
from pathlib import Path

EU_OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EU_OPS_ROOT))

from services import ow_new_order


def _draft(state, country="India"):
    return {
        "site": "in.optiwar.com",
        "customer": {
            "mode": "new",
            "name": "Customer",
            "email": "customer@example.com",
            "phone": "1234567890",
        },
        "address": {
            "mode": "new",
            "address": "1 Main Street",
            "state": state,
            "zipcode": "110001",
            "country": country,
        },
        "lines": [],
    }


def test_validation_rejects_invalid_india_state_and_normalizes_alias():
    invalid = _draft("Berlin")
    assert "State: choose one of the 36 Indian states" in (
        ow_new_order.validate_draft(invalid))

    accepted = _draft("up")
    errors = ow_new_order.validate_draft(accepted)
    assert not any(error.startswith("State:") for error in errors)
    assert accepted["address"]["state"] == "Uttar Pradesh"


def test_sql_and_create_insert_store_canonical_state():
    draft = _draft("up")
    sql = ow_new_order.build_sql(draft, "930001")
    assert "'Uttar Pradesh'" in sql

    class Cursor:
        lastrowid = 10

        def __init__(self):
            self.calls = []

        def execute(self, query, params=None):
            self.calls.append((query, params))

    cursor = Cursor()
    ow_new_order._insert_order(cursor, draft, "930001", "test")
    address_insert = next(
        (query, params) for query, params in cursor.calls
        if "INSERT INTO customers_address" in query)
    assert address_insert[1][3] == "Uttar Pradesh"


def test_bharat_hr_normalizes_in_validation_and_both_write_paths():
    validated = _draft("HR", "Bharat")
    errors = ow_new_order.validate_draft(validated)
    assert not any(error.startswith("State:") for error in errors)
    assert validated["address"]["state"] == "Haryana"

    sql = ow_new_order.build_sql(_draft("HR", "Bharat"), "930002")
    assert "'Haryana'" in sql

    class Cursor:
        lastrowid = 10

        def __init__(self):
            self.calls = []

        def execute(self, query, params=None):
            self.calls.append((query, params))

    cursor = Cursor()
    ow_new_order._insert_order(cursor, _draft("HR", "Bharat"), "930002", "test")
    address_insert = next(
        (query, params) for query, params in cursor.calls
        if "INSERT INTO customers_address" in query)
    assert address_insert[1][3] == "Haryana"
