import sys
from pathlib import Path

import pytest

EU_OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EU_OPS_ROOT))

from services import ow_new_order


def _draft(state, country="India", site="in.optiwar.com"):
    return {
        "site": site,
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


def _existing_draft(state, country="India", site="in.optiwar.com"):
    draft = _draft(state, country=country, site=site)
    draft["customer"] = {
        "mode": "existing",
        "customer_id": 7,
        "name": "Customer",
        "email": "customer@example.com",
        "phone": "1234567890",
    }
    draft["address"] = {
        "mode": "existing",
        "address_id": 9,
        "state": state,
        "country": country,
    }
    draft["lines"] = [{"product_id": 1, "unit_price": 100, "qty": 1}]
    return draft


class ExistingAddressCursor:
    lastrowid = 10

    def __init__(self, state, country="India"):
        self.state = state
        self.country = country
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchone(self):
        query = self.calls[-1][0]
        if "SELECT customer_id FROM customers" in query:
            return {"customer_id": 7}
        if "SELECT address_id, state, country FROM customers_address" in query:
            return {
                "address_id": 9,
                "state": self.state,
                "country": self.country,
            }
        if "SELECT address_id FROM customers_address" in query:
            return {"address_id": 9}
        return None


def _patch_saved_address(monkeypatch, state, country="India"):
    monkeypatch.setattr(
        ow_new_order, "get_customer_addresses",
        lambda customer_id: [{
            "address_id": 9,
            "state": state,
            "country": country,
        }])


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
    assert "UPDATE customers_address" not in sql

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
    assert "UPDATE customers_address" not in sql

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


def test_export_site_does_not_validate_or_normalize_india_country_state():
    for state in ("Berlin", "up"):
        draft = _draft(state, country="India", site="optiwar.com")
        errors = ow_new_order.validate_draft(draft)
        assert not any(error.startswith("State:") for error in errors)
        assert draft["address"]["state"] == state

        sql = ow_new_order.build_sql(draft, "930003")
        assert f"'{state}'" in sql
        assert "UPDATE customers_address" not in sql

        class Cursor:
            lastrowid = 10

            def __init__(self):
                self.calls = []

            def execute(self, query, params=None):
                self.calls.append((query, params))

        cursor = Cursor()
        ow_new_order._insert_order(cursor, draft, "930003", "test")
        address_insert = next(
            (query, params) for query, params in cursor.calls
            if "INSERT INTO customers_address" in query)
        assert address_insert[1][3] == state


def test_existing_india_address_rejects_unrecognized_saved_state(monkeypatch):
    _patch_saved_address(monkeypatch, "Berlin")
    draft = _existing_draft("Berlin")

    errors = ow_new_order.validate_draft(draft)

    assert ("Saved address state 'Berlin' is not one of the 36 Indian states: "
            "add a new address with the correct state.") in errors


    sql = ow_new_order.build_sql(draft, "930004")
    assert "UPDATE customers_address" not in sql

    cursor = ExistingAddressCursor("Berlin")
    with pytest.raises(ValueError) as exc_info:
        ow_new_order._insert_order(cursor, draft, "930004", "test")
    assert exc_info.value.args[0] == (
        "Saved address state 'Berlin' is not one of the 36 Indian states: "
        "add a new address with the correct state.")
    assert not any("UPDATE customers_address" in query for query, _ in cursor.calls)


def test_existing_india_address_requires_exact_canonical_state(monkeypatch):
    _patch_saved_address(monkeypatch, "Chhattisgarh")
    draft = _existing_draft("Chhattisgarh")

    assert not ow_new_order.validate_draft(draft)
    sql = ow_new_order.build_sql(draft, "930005")
    assert "UPDATE customers_address" not in sql

    cursor = ExistingAddressCursor("Chhattisgarh")
    ow_new_order._insert_order(cursor, draft, "930005", "test")
    assert not any("UPDATE customers_address" in query for query, _ in cursor.calls)


def test_existing_india_address_requires_new_address_for_known_variant(monkeypatch):
    _patch_saved_address(monkeypatch, "Chattisgarh")
    draft = _existing_draft("Chattisgarh")
    expected = (
        "Saved address state 'Chattisgarh' must read 'Chhattisgarh': "
        "add a new address with state Chhattisgarh.")

    assert ow_new_order.validate_draft(draft) == [expected]
    sql = ow_new_order.build_sql(draft, "930006")
    assert "UPDATE customers_address" not in sql

    cursor = ExistingAddressCursor("Chattisgarh")
    with pytest.raises(ValueError) as exc_info:
        ow_new_order._insert_order(cursor, draft, "930006", "test")
    assert exc_info.value.args[0] == expected
    assert not any("UPDATE customers_address" in query for query, _ in cursor.calls)


def test_existing_india_address_requires_indian_country(monkeypatch):
    _patch_saved_address(monkeypatch, "GA", country="United States")
    draft = _existing_draft("GA", country="United States")
    expected = "in.optiwar.com orders must ship to an Indian address."

    assert expected in ow_new_order.validate_draft(draft)
    sql = ow_new_order.build_sql(draft, "930007")
    assert "UPDATE customers_address" not in sql

    cursor = ExistingAddressCursor("GA", country="United States")
    with pytest.raises(ValueError) as exc_info:
        ow_new_order._insert_order(cursor, draft, "930007", "test")
    assert exc_info.value.args[0] == expected
    assert not any("UPDATE customers_address" in query for query, _ in cursor.calls)


def test_export_existing_addresses_are_not_validated_or_changed(monkeypatch):
    def unexpected_lookup(_customer_id):
        raise AssertionError("export site must not look up saved state")

    monkeypatch.setattr(
        ow_new_order, "get_customer_addresses", unexpected_lookup)
    for state, country in (
            ("Chattisgarh", "India"),
            ("GA", "United States")):
        draft = _existing_draft(
            state, country=country, site="optiwar.com")
        assert not ow_new_order.validate_draft(draft)
        assert draft["address"]["state"] == state

        sql = ow_new_order.build_sql(draft, "930008")
        assert "UPDATE customers_address" not in sql

        cursor = ExistingAddressCursor(state, country=country)
        ow_new_order._insert_order(cursor, draft, "930008", "test")
        assert not any("UPDATE customers_address" in query
                       for query, _ in cursor.calls)
        assert any("SELECT address_id FROM customers_address" in query
                   for query, _ in cursor.calls)
