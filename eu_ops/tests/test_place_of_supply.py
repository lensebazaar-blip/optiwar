import asyncio
import csv
import importlib.util
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

from jinja2 import DictLoader, Environment

ROOT = Path(__file__).resolve().parents[2]
EU_OPS_ROOT = ROOT / "eu_ops"
sys.path.insert(0, str(EU_OPS_ROOT))

from services import india_states, optiwar_orders, place_of_supply


_main_module = None


def _load_main():
    global _main_module
    if _main_module is None:
        path = EU_OPS_ROOT / "main.py"
        spec = importlib.util.spec_from_file_location(
            "eu_ops_main_place_of_supply_test", path)
        module = importlib.util.module_from_spec(spec)
        missing = object()
        previous_gst_irn = sys.modules.get("gst_irn", missing)
        gst_irn_stub = ModuleType("gst_irn")
        gst_irn_stub.Session = object
        sys.modules["gst_irn"] = gst_irn_stub
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(spec.name, None)
            raise
        finally:
            if previous_gst_irn is missing:
                sys.modules.pop("gst_irn", None)
            else:
                sys.modules["gst_irn"] = previous_gst_irn
        _main_module = module
    return _main_module


def _pincodes():
    path = EU_OPS_ROOT / "data" / "gst_pincodes.csv"
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def test_pincode_data_has_all_states_codes_and_expected_coverage():
    path = EU_OPS_ROOT / "data" / "gst_pincodes.csv"
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        assert reader.fieldnames == [
            "pincode", "state", "gst_state_code", "district", "other_states",
            "other_districts", "post_offices", "delivery_offices", "check",
        ]
        rows = list(reader)

    assert len(rows) == 19_300
    assert sum(row["check"] == "PIN spans more than one state" for row in rows) == 46
    for row in rows:
        assert row["state"] in india_states.STATE_NAMES
        assert row["gst_state_code"] == india_states.GST_CODE[row["state"]]
        for state in filter(None, row["other_states"].split(";")):
            assert state in india_states.STATE_NAMES


def test_resolver_prefers_pin_and_adds_customer_state_note():
    assert place_of_supply.resolve("122004", "", "") == {
        "state": "Haryana", "code": india_states.GST_CODE["Haryana"],
        "source": "pin", "note": "",
    }
    up = place_of_supply.resolve("201304", "Delhi", "")
    assert up == {
        "state": "Uttar Pradesh",
        "code": india_states.GST_CODE["Uttar Pradesh"],
        "source": "pin",
        "note": "Customer chose Delhi; pin 201304 is in Uttar Pradesh",
    }
    chhattisgarh = place_of_supply.resolve("495450", "Chattisgarh", "")
    assert chhattisgarh == {
        "state": "Chhattisgarh",
        "code": india_states.GST_CODE["Chhattisgarh"],
        "source": "pin",
        "note": "",
    }


def test_resolver_requires_state_for_multistate_pin():
    row = next(row for row in _pincodes()
               if row["check"] == "PIN spans more than one state")
    candidates = [row["state"], *filter(None, row["other_states"].split(";"))]
    resolved = place_of_supply.resolve(row["pincode"], candidates[0], "")
    assert resolved["state"] == candidates[0]
    assert resolved["source"] == "pin+state"
    other_state = next(state for state in india_states.STATE_NAMES
                       if state not in candidates)
    assert place_of_supply.resolve(row["pincode"], other_state, "") is None


def test_resolver_falls_back_to_state_then_unambiguous_district():
    state = place_of_supply.resolve("000000", "UP", "")
    assert state["state"] == "Uttar Pradesh"
    assert state["source"] == "state"

    district_states = defaultdict(set)
    for row in _pincodes():
        if row["district"].strip():
            district_states[row["district"].strip()].add(row["state"])
    single_district = next(name for name, states in district_states.items()
                           if len(states) == 1)
    city = place_of_supply.resolve("", "", f"near {single_district} market")
    assert city["state"] == next(iter(district_states[single_district]))
    assert city["source"] == "city"


def test_resolver_rejects_ambiguous_district_and_empty_evidence():
    district_states = defaultdict(set)
    for row in _pincodes():
        if row["district"].strip():
            district_states[row["district"].strip()].add(row["state"])
    ambiguous_district = next(name for name, states in district_states.items()
                              if len(states) > 1)
    assert place_of_supply.resolve("", "", ambiguous_district) is None
    assert place_of_supply.resolve("", "", "") is None


class _SQLCursor:
    def __init__(self, pos_row=None):
        self.pos_row = pos_row
        self.calls = []
        self.result_one = None
        self.result_all = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))
        self.result_one = None
        self.result_all = []
        if "SELECT order_id, place_of_supply" in query:
            self.result_one = self.pos_row

    def fetchone(self):
        return self.result_one

    def fetchall(self):
        return self.result_all


def test_freeze_invoice_pos_upserts_and_returns_row(monkeypatch):
    frozen = {
        "order_id": "930001", "place_of_supply": "Uttar Pradesh",
        "gst_state_code": "09", "source": "pin", "pin": "201304",
        "customer_state": "Delhi", "note": "Customer chose Delhi",
    }
    cursor = _SQLCursor(pos_row=frozen)
    monkeypatch.setattr(optiwar_orders, "ow_cursor", lambda: cursor)
    monkeypatch.setattr(optiwar_orders, "ow_ops_cursor", lambda: cursor)

    actual = optiwar_orders.freeze_invoice_pos(
        "930001",
        {"state": "Haryana", "code": "06", "source": "pin", "note": ""},
        "122004", "Haryana", 7)

    assert actual == frozen
    statements = [query for query, _ in cursor.calls]
    assert any("CREATE TABLE IF NOT EXISTS ops_invoice_pos" in query
               for query in statements)
    insert = next(query for query in statements if "INSERT INTO ops_invoice_pos" in query)
    assert "place_of_supply" in insert
    assert "ON DUPLICATE KEY UPDATE" in insert
    mutations = [query for query in statements if re.search(
        r"\b(?:UPDATE\b|DELETE\s+FROM\b)", query, re.IGNORECASE)]
    assert mutations == [insert]
    assert not any(re.search(
        r"\b(?:UPDATE|DELETE\s+FROM)\s+customers_address\b",
        query, re.IGNORECASE) for query in statements)
    assert not any(re.search(
        r"\bDELETE\s+FROM\s+ops_invoice_pos\b",
        query, re.IGNORECASE) for query in statements)


class _DeletedAddressCursor:
    def __init__(self):
        self.calls = []
        self.one = None
        self.many = []
        self.line = {
            "order_line_id": 1, "order_id": "930002",
            "date_created": datetime(2026, 11, 27), "order_quantity": 1,
            "order_total": 1000, "product_id": 1, "rx_id": None,
            "customer_id": 7, "address_id": 9, "site_from": "in.optiwar.com",
            "customer_name": "Customer", "customer_email": "test@example.com",
            "customer_phone": "1234567890", "product_name": "Frame",
            "product_code": "F1", "product_price": 1000,
            "product_special_price": None, "product_category": "Frames",
            "product_color": "Black", "product_size": "M",
            "product_style": "", "product_material": "", "product_type": "",
            "box_number": "", "product_image": "",
        }

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))
        self.one = None
        self.many = []
        if "FROM orders o" in query:
            self.many = [self.line]
        elif "FROM customers_address" in query and "address_id = %s" in query:
            self.one = None
        elif "FROM customers_address" in query:
            self.one = {
                "address": "Latest saved address", "state": "Delhi",
                "zipcode": "110001", "country": "India",
            }
        elif "FROM order_shipping" in query:
            self.one = {"state": "Haryana", "zipcode": "122004"}
        elif "FROM order_status" in query:
            self.one = {"order_status_name": "Processing"}
        elif "FROM payment_collector" in query:
            self.one = {"status": "TXN_SUCCESS"}
        elif "FROM order_history" in query:
            self.many = []
        else:
            raise AssertionError(f"Unexpected SQL: {query}")

    def fetchone(self):
        return self.one

    def fetchall(self):
        return list(self.many)


class _SavedAddressCursor(_DeletedAddressCursor):
    def execute(self, query, params=None):
        super().execute(query, params)
        if "FROM customers_address" in query and "address_id = %s" in query:
            self.one = {
                "address": "Street", "address2": "Gurugram",
                "state": "", "zipcode": "",
            }


def test_get_order_uses_shipping_copy_for_deleted_order_address(monkeypatch):
    cursor = _DeletedAddressCursor()
    monkeypatch.setattr(optiwar_orders, "ow_cursor", lambda: cursor)
    monkeypatch.setattr(optiwar_orders, "get_upgrade", lambda order_id: None)

    order = optiwar_orders.get_order("930002")

    assert order["site_from"] == "in.optiwar.com"
    assert order["shipping_copy"] == {"state": "Haryana", "zipcode": "122004"}
    assert order["pos_address_missing"] is True
    assert order["shipping_zone"] == "Delhi"
    assert order["shipping_postcode"] == "110001"
    assert not any(re.search(
        r"\b(?:UPDATE|DELETE\s+FROM)\s+(?:ops_invoice_pos|customers_address)\b",
        query, re.IGNORECASE) for query, _ in cursor.calls)


def test_get_order_builds_invoice_address_text_with_address2(monkeypatch):
    cursor = _SavedAddressCursor()
    monkeypatch.setattr(optiwar_orders, "ow_cursor", lambda: cursor)
    monkeypatch.setattr(optiwar_orders, "get_upgrade", lambda order_id: None)

    order = optiwar_orders.get_order("930002")

    assert order["pos_address"] == {
        "state": "", "zipcode": "", "address_text": "Street Gurugram",
    }
    assert order["shipping_address_2"] == ""
    assert order["shipping_copy"] == {"state": "Haryana", "zipcode": "122004"}


def _patch_issue_route(monkeypatch, order):
    app = _load_main()
    events = []
    monkeypatch.setattr(app.ow_orders, "get_order", lambda order_id: order)
    monkeypatch.setattr(app.ow_orders, "get_invoice_date", lambda order_id: None)
    monkeypatch.setattr(
        app.invoice_dates, "resolve_choice",
        lambda *args: (date(2026, 11, 27), "today", None))

    def freeze(order_id, resolution, pin, customer_state, created_by):
        events.append(("freeze", resolution, pin, customer_state, created_by))
        return {"order_id": order_id, "place_of_supply": resolution["state"]}

    def issue(order_id, stamp, changed_by):
        events.append(("invoice", order_id, stamp, changed_by))
        return True

    monkeypatch.setattr(app.ow_orders, "freeze_invoice_pos", freeze)
    monkeypatch.setattr(app.ow_orders, "issue_invoice_date", issue)
    monkeypatch.setattr(app.ow_orders, "get_invoice",
                        lambda order_id: {"invoice_no": order_id})
    monkeypatch.setattr(app.auth, "log_activity", lambda *args: None)
    monkeypatch.setattr(app.templates, "TemplateResponse",
                        lambda **kwargs: kwargs)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    return app, events, request


def _call_issue(app, request, order_id="930003", pos_state=""):
    return asyncio.run(app.ow_invoice_issue(
        request, order_id, next="invoice", choice="today", print_date="",
        pos_state=pos_state, user={"id": 7}))


def test_issue_route_freezes_pos_before_invoice_and_uses_deleted_address_copy(monkeypatch):
    order = {
        "site_from": "in.optiwar.com",
        "pos_address_missing": True,
        "shipping_copy": {"state": "Haryana", "zipcode": "122004"},
        "shipping_zone": "Delhi",
        "shipping_postcode": "110001",
        "shipping_address_1": "Old display address",
        "shipping_address_2": "",
        "date_added": datetime(2026, 11, 26),
    }
    app, events, request = _patch_issue_route(monkeypatch, order)

    response = _call_issue(app, request)

    assert response.status_code == 303
    assert [event[0] for event in events] == ["freeze", "invoice"]
    _, resolution, pin, customer_state, created_by = events[0]
    assert (resolution["state"], resolution["source"]) == ("Haryana", "pin")
    assert (pin, customer_state, created_by) == ("122004", "Haryana", 7)


def test_issue_route_prefers_checkout_shipping_copy_to_edited_address(monkeypatch):
    order = {
        "site_from": "in.optiwar.com",
        "pos_address": {
            "state": "Delhi", "zipcode": "110001",
            "address_text": "Edited saved address, New Delhi",
        },
        "shipping_copy": {"state": "Haryana", "zipcode": "122004"},
        "date_added": datetime(2026, 11, 26),
    }
    app, events, request = _patch_issue_route(monkeypatch, order)

    response = _call_issue(app, request)

    assert response.status_code == 303
    _, resolution, pin, customer_state, _ = events[0]
    assert (resolution["state"], resolution["source"]) == ("Haryana", "pin")
    assert (pin, customer_state) == ("122004", "Haryana")


def test_issue_route_uses_order_address_when_shipping_copy_is_absent(monkeypatch):
    order = {
        "site_from": "in.optiwar.com",
        "pos_address": {
            "state": "Haryana", "zipcode": "122004",
            "address_text": "Gurugram",
        },
        "date_added": datetime(2026, 11, 26),
    }
    app, events, request = _patch_issue_route(monkeypatch, order)

    response = _call_issue(app, request)

    assert response.status_code == 303
    _, resolution, pin, customer_state, _ = events[0]
    assert (resolution["state"], resolution["source"]) == ("Haryana", "pin")
    assert (pin, customer_state) == ("122004", "Haryana")


def test_issue_retry_can_replace_pos_after_issue_failure(monkeypatch):
    order = {
        "site_from": "in.optiwar.com",
        "pos_address": {"state": "", "zipcode": "000000", "address_text": ""},
        "date_added": datetime(2026, 11, 26),
    }
    app, events, request = _patch_issue_route(monkeypatch, order)
    frozen = {}
    outcomes = iter([False, True])

    def upsert(order_id, resolution, pin, customer_state, created_by):
        frozen[order_id] = {
            "place_of_supply": resolution["state"],
            "source": resolution["source"],
            "pin": pin,
            "customer_state": customer_state,
        }
        events.append(("freeze", resolution, pin, customer_state, created_by))
        return {"order_id": order_id, **frozen[order_id]}

    def issue(order_id, stamp, changed_by):
        events.append(("invoice", order_id, stamp, changed_by))
        return next(outcomes)

    monkeypatch.setattr(app.ow_orders, "freeze_invoice_pos", upsert)
    monkeypatch.setattr(app.ow_orders, "issue_invoice_date", issue)

    first = _call_issue(app, request, pos_state="Haryana")
    assert first.status_code == 303
    assert frozen["930003"]["place_of_supply"] == "Haryana"

    retry = _call_issue(app, request, pos_state="Uttar Pradesh")
    assert retry.status_code == 303
    assert frozen["930003"]["place_of_supply"] == "Uttar Pradesh"
    assert [event[1]["state"] for event in events if event[0] == "freeze"] == [
        "Haryana", "Uttar Pradesh",
    ]


def test_issue_route_with_existing_invoice_does_not_touch_pos(monkeypatch):
    order = {
        "site_from": "in.optiwar.com",
        "pos_address": {"state": "", "zipcode": "000000", "address_text": ""},
        "date_added": datetime(2026, 11, 26),
    }
    app, events, request = _patch_issue_route(monkeypatch, order)
    monkeypatch.setattr(
        app.ow_orders, "get_invoice_date",
        lambda order_id: datetime(2026, 11, 27))

    def unexpected_write(*args, **kwargs):
        raise AssertionError("existing invoices must not touch place of supply")

    monkeypatch.setattr(app.ow_orders, "freeze_invoice_pos", unexpected_write)

    response = _call_issue(app, request)

    assert response.status_code == 303
    assert events == []


def test_issue_route_prompts_for_unknown_pos_without_inserts(monkeypatch):
    order = {
        "site_from": "in.optiwar.com", "shipping_zone": "",
        "shipping_postcode": "", "shipping_address_1": "",
        "shipping_address_2": "", "date_added": datetime(2026, 11, 26),
    }
    app, events, request = _patch_issue_route(monkeypatch, order)

    response = _call_issue(app, request)

    context = response["context"]
    assert context["error"] == (
        "Place of supply unknown: pin , state ''. Choose the state.")
    assert context["pos_states"] == india_states.STATE_NAMES
    assert events == []


def test_issue_route_manual_state_uses_ops_source_and_rejects_noncanonical(monkeypatch):
    order = {
        "site_from": "in.optiwar.com", "shipping_zone": "",
        "shipping_postcode": "000000", "shipping_address_1": "",
        "shipping_address_2": "", "date_added": datetime(2026, 11, 26),
    }
    app, events, request = _patch_issue_route(monkeypatch, order)

    response = _call_issue(app, request, pos_state="Uttar Pradesh")

    assert response.status_code == 303
    assert events[0][1]["source"] == "ops"
    assert events[0][1]["state"] == "Uttar Pradesh"

    app, events, request = _patch_issue_route(monkeypatch, order)
    response = _call_issue(app, request, pos_state="up")
    assert response["context"]["error"] == (
        "Place of supply unknown: pin 000000, state ''. Choose the state.")
    assert events == []


def test_issue_route_export_site_does_not_freeze_pos(monkeypatch):
    order = {
        "site_from": "optiwar.com", "shipping_zone": "Berlin",
        "shipping_postcode": "", "date_added": datetime(2026, 11, 26),
    }
    app, events, request = _patch_issue_route(monkeypatch, order)

    response = _call_issue(app, request)

    assert response.status_code == 303
    assert [event[0] for event in events] == ["invoice"]


def test_issue_route_existing_invoice_redirects_before_pos_freeze(monkeypatch):
    order = {"site_from": "in.optiwar.com"}
    app, events, request = _patch_issue_route(monkeypatch, order)
    monkeypatch.setattr(app.ow_orders, "get_invoice_date",
                        lambda order_id: datetime(2026, 11, 27))

    response = _call_issue(app, request)

    assert response.status_code == 303
    assert events == []


def test_frozen_pos_controls_gst_supply_type_and_invoice_displays_code():
    app = _load_main()
    order = {
        "shipping_zone": "Haryana",
        "products": [{"model": "F1", "total": 1050, "quantity": 1}],
    }
    haryana = app._build_gst_invoice(order, {
        "place_of_supply": "Haryana", "gst_state_code": "06"})
    assert haryana["buyer_state"] == "Haryana"
    assert haryana["intra"] is True
    assert haryana["lines"][0]["cgst"] > 0
    assert haryana["lines"][0]["sgst"] > 0

    uttar_pradesh = app._build_gst_invoice(order, {
        "place_of_supply": "Uttar Pradesh", "gst_state_code": "09"})
    assert uttar_pradesh["buyer_state"] == "Uttar Pradesh"
    assert uttar_pradesh["intra"] is False
    assert uttar_pradesh["lines"][0]["igst"] > 0
    template = (EU_OPS_ROOT / "templates" / "pages" / "ow_invoice_gst.html").read_text()
    assert "Place of Supply:" in template
    assert "gst.place_of_supply_code" in template


def test_invoice_issue_prompt_renders_one_36_state_select_only_when_requested():
    template_dir = EU_OPS_ROOT / "templates"
    prompt_source = (template_dir / "pages" / "invoice_issue_date.html").read_text()
    env = Environment(loader=DictLoader({
        "base.html": "{% block title %}{% endblock %}{% block content %}{% endblock %}",
        "pages/invoice_issue_date.html": prompt_source,
    }))
    template = env.get_template("pages/invoice_issue_date.html")
    common = {
        "base": "", "order_label": "OW-1", "order_date": "26-Nov-2026",
        "today": "27-Nov-2026", "min_iso": "2026-11-26",
        "max_iso": "2026-11-27", "back_url": "/", "action_url": "/issue",
        "error": "", "entered": "",
    }
    rendered = template.render(
        **common, pos_states=india_states.STATE_NAMES, pos_state="")
    assert '<select id="pos_state" name="pos_state"' in rendered
    assert rendered.count("<option value=") == 37
    for state in india_states.STATE_NAMES:
        assert f'<option value="{state}"' in rendered
    default_render = template.render(**common)
    assert '<select id="pos_state" name="pos_state"' not in default_render


def test_place_of_supply_note_is_on_order_detail_not_invoice():
    detail = (EU_OPS_ROOT / "templates" / "pages" / "ow_order_detail.html").read_text()
    invoice = (EU_OPS_ROOT / "templates" / "pages" / "ow_invoice_gst.html").read_text()
    assert "invoice_pos.note" in detail
    assert "invoice_pos.note" not in invoice
