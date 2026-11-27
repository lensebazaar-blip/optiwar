import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eu_ops"))

from services import ow_new_order


def draft(site, state, country):
    return {
        "site": site,
        "customer": {
            "mode": "new",
            "name": "Customer",
            "email": "customer@example.test",
            "phone": "1234567890",
        },
        "address": {
            "mode": "new",
            "address": "12 Example Road",
            "state": state,
            "zipcode": "123456",
            "country": country,
        },
        "lines": [{"product_id": "123", "total_override": "100"}],
    }


def test_india_draft_state_is_canonicalized():
    data = draft("in.optiwar.com", "Chattisgarh", "India")
    assert ow_new_order.validate_draft(data) == []
    assert data["address"]["state"] == "Chhattisgarh"


def test_india_draft_rejects_unknown_state():
    data = draft("in.optiwar.com", "Zz", "India")
    assert "Please choose your state from the list" in ow_new_order.validate_draft(data)
    assert data["address"]["state"] == "Zz"


def test_non_india_draft_keeps_free_text_region():
    data = draft("optiwar.com", "Île-de-France", "France")
    assert ow_new_order.validate_draft(data) == []
    assert data["address"]["state"] == "Île-de-France"
