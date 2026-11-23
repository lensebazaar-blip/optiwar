"""Frame-offer transactions and the QC restock guard, with DB-API fakes."""

import hashlib
import importlib.util
import json
import os
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import Blueprint, Flask

ROOT = Path(__file__).resolve().parents[1]


def _package():
    name = "frame_offer_test_pkg"
    if name in sys.modules:
        return name, sys.modules[name + ".frame_offers"]
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT)]
    sys.modules[name] = package
    fsi = types.ModuleType(name + ".face_scan_invites")
    fsi.hash_token = lambda token: hashlib.sha256(token.encode()).hexdigest()
    sys.modules[fsi.__name__] = fsi
    refunds = types.ModuleType(name + ".refunds")
    refunds.PROCESSED = "PROCESSED"
    refunds.FAILED = "FAILED"
    refunds.STATUS_FULL = "Refunded"
    refunds.STATUS_PARTIAL = "Partially Refunded"

    def ledger_refunds(cursor, order_id):
        cursor.execute(
            "SELECT refund_id, provider_refund_id, amount_minor, currency, status, "
            "refund_type, reason_code, requested_by, service_identity, "
            "idempotency_key, requested_at, completed_at, error_text "
            "FROM order_refunds WHERE order_id=%s ORDER BY refund_id DESC",
            (order_id,))
        return list(cursor.fetchall())

    refunds.ledger_refunds = ledger_refunds
    refunds.committed_minor = lambda rows: sum(
        int(row["amount_minor"]) for row in rows if row["status"] != refunds.FAILED)
    sys.modules[refunds.__name__] = refunds
    paid = types.ModuleType(name + ".paid_orders")

    def order_amount_minor(cursor, order_id):
        state = cursor.db.state
        if state.get("delivery_fee_minor") is not None:
            return (int(round(float(state["line"]["order_total"]) * 100))
                    + int(state["delivery_fee_minor"]))
        return state["paid_total_minor"]

    paid.order_amount_minor = order_amount_minor

    def mark_out_of_stock(cursor, product_id):
        cursor.execute(
            "UPDATE products SET product_status='OUT_OF_STOCK', status_changed_at=NOW(), "
            "status_changed_by='system-stock', status_reason='auto: stock depleted' "
            "WHERE product_id=%s AND product_quantity<=0 AND product_status='ACTIVE'",
            (product_id,))
        if cursor.rowcount == 1:
            cursor.execute(
                "INSERT INTO product_status_history "
                "(product_id, old_status, new_status, reason, changed_by) "
                "VALUES (%s,'ACTIVE','OUT_OF_STOCK','auto: stock depleted','system-stock')",
                (product_id,))

    paid.mark_out_of_stock = mark_out_of_stock
    sys.modules[paid.__name__] = paid
    spec = importlib.util.spec_from_file_location(
        name + ".frame_offers", ROOT / "frame_offers.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return name, module


PACKAGE, offers = _package()


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.result = None
        self.rows = []
        self.rowcount = 0
        self.queries = []

    def execute(self, sql, params=()):
        params = tuple(params or ())
        self.queries.append((sql, params))
        query = " ".join(sql.lower().split())
        state = self.db.state
        self.result, self.rows, self.rowcount = None, [], 0
        if query.startswith("create table"):
            return
        if query.startswith("select o.order_line_id"):
            self.result = dict(state["line"])
        elif query.startswith("select order_quantity from orders"):
            self.result = {"order_quantity": state["line"]["order_quantity"]}
        elif query.startswith("update frame_swap_offers set opened_at=coalesce"):
            if state.get("offer"):
                state["offer"]["opened_at"] = params[0]
                self.rowcount = 1
        elif query.startswith("select refund_id, provider_refund_id"):
            self.rows = [dict(refund) for refund in state["refunds"]]
        elif query.startswith("select order_status_name from order_status"):
            if state.get("order_status_name"):
                self.result = {"order_status_name": state["order_status_name"]}
        elif "join products f on f.product_id" in query:
            required = int(params[1]) if len(params) > 1 else 1
            self.rows = [dict(row) for row in state["matches"]
                         if int(row.get("product_quantity") or 0) >= required]
        elif query.startswith("select product_id, product_image"):
            self.rows = [{"product_id": row["product_id"],
                          "product_image": row.get("product_image", "")}
                         for row in state["matches"] if row["product_id"] in params]
        elif query.startswith("update frame_swap_offers set status='cancelled'"):
            if state.get("offer") and state["offer"]["status"] == "OFFERED":
                state["offer"]["status"] = "CANCELLED"
                self.rowcount = 1
        elif query.startswith("insert into frame_swap_offers"):
            self.db.next_id += 1
            (offer_uuid, order_id, line_id, reason, failed_id, failed_price,
             candidates_json, token_hash, created_by, created_at, expires_at) = params
            state["offer"] = {
                "id": self.db.next_id, "offer_uuid": offer_uuid, "order_id": order_id,
                "order_line_id": line_id, "reason": reason,
                "failed_product_id": failed_id, "failed_price_minor": failed_price,
                "candidates_json": candidates_json, "token_hash": token_hash,
                "status": "OFFERED", "created_by": created_by,
                "created_at": created_at, "expires_at": expires_at,
                "opened_at": None, "reminder_sent_at": None, "alerted_at": None,
            }
            self.rowcount = 1
        elif query.startswith("insert ignore into stock_restocks"):
            state["guard"] = params
            self.rowcount = 1
        elif query.startswith("insert into order_history"):
            state["history"].append(params[0])
            self.rowcount = 1
        elif query.startswith("select * from frame_swap_offers where offer_uuid"):
            if state.get("offer") and state["offer"]["offer_uuid"] == params[0]:
                self.result = dict(state["offer"])
        elif query.startswith("select * from frame_swap_offers where token_hash"):
            if state.get("offer") and state["offer"]["token_hash"] == params[0]:
                self.result = dict(state["offer"])
        elif query.startswith("select * from frame_swap_offers where id"):
            if state.get("offer") and state["offer"]["id"] == params[0]:
                self.result = dict(state["offer"])
        elif query.startswith("update frame_swap_offers set status='expired'"):
            state["offer"]["status"] = "EXPIRED"
            self.rowcount = 1
        elif query.startswith("update frame_swap_offers set status='refund_requested'"):
            state["offer"]["status"] = "REFUND_REQUESTED"
            state["offer"]["decided_at"] = params[0]
            self.rowcount = 1
        elif query.startswith("update products set product_quantity=product_quantity-"):
            product = state["product"]
            qty, _product_id, minimum = params
            if (product["product_status"] == "ACTIVE"
                    and product["product_quantity"] >= minimum):
                product["product_quantity"] -= qty
                self.rowcount = 1
        elif query.startswith("select product_quantity from products"):
            self.result = {"product_quantity": state["product"]["product_quantity"]}
        elif query.startswith("update products set product_status='out_of_stock'"):
            product = state["product"]
            if product["product_quantity"] <= 0 and product["product_status"] == "ACTIVE":
                product["product_status"] = "OUT_OF_STOCK"
                self.rowcount = 1
        elif query.startswith("insert into product_status_history"):
            state["status_history"].append(params)
            self.rowcount = 1
        elif query.startswith("update orders set product_id"):
            state["line"]["product_id"] = params[0]
            self.rowcount = 1
        elif query.startswith("delete from stock_restocks"):
            state["guard"] = None
            self.rowcount = 1
        elif query.startswith("update frame_swap_offers set status=%s, chosen_product_id"):
            status, pid, diff, settlement, decided_at, _offer_id = params
            state["offer"].update(
                status=status, chosen_product_id=pid, price_diff_minor=diff,
                settlement=settlement, decided_at=decided_at)
            self.rowcount = 1
        else:
            raise AssertionError("unexpected SQL: %s" % sql)

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.rows


class FakeDB:
    def __init__(self):
        self.next_id = 0
        self.commits = 0
        self.rollbacks = 0
        self.begins = 0
        self.state = {
            "line": {"order_line_id": 11, "order_id": "O-1",
                     "product_id": 10, "order_quantity": 1,
                     "order_total": 100, "site_from": "in.optiwar.com",
                     "product_code": "AB28", "product_name": "No Art",
                     "product_special_price": "100.00"},
            "product": {"product_id": 20, "product_quantity": 1,
                        "product_status": "ACTIVE"},
            "matches": [{
                "product_id": 20, "product_code": "AB27", "product_name": "Replacement",
                "product_size": "52-18-136", "color_display": "Black",
                "product_special_price": "100.00", "product_quantity": 2,
                "product_image": "./catalog/ab27.jpg"}],
            "offer": None, "guard": None, "history": [], "status_history": [],
            "refunds": [], "paid_total_minor": 10000, "order_status_name": "",
        }
        self.cursor_obj = FakeCursor(self)

    def cursor(self):
        return self.cursor_obj

    def begin(self):
        self.begins += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class MatchQueryTests(unittest.TestCase):
    def test_site_switch_is_the_only_query_change_and_product_id_is_parameterized(self):
        expected = (offers.QUERY
                    .replace("f.product_id = 667", "f.product_id = %s")
                    .replace("p.product_quantity > 0",
                             "p.product_quantity >= %s")
                    .replace("AND p.product_id <> f.product_id",
                             "AND p.product_id <> f.product_id"
                             + offers.brand_compliance.sql_exclude("p")))
        self.assertEqual(offers.match_sql("in.optiwar.com"), expected)
        self.assertEqual(offers.match_sql("optiwar.com"),
                         expected.replace("p.sell_on_in = 1", "p.sell_on_com = 1"))
        cursor = FakeDB().cursor()
        offers.matches(cursor, 774, "in.optiwar.com")
        self.assertEqual(cursor.queries[-1][1], (774, 1))
        self.assertIn("f.product_id = %s", cursor.queries[-1][0])
        self.assertIn("p.product_quantity >= %s", cursor.queries[-1][0])
        self.assertIn("p.sell_on_in = 1", cursor.queries[-1][0])

    def test_matches_filters_for_order_quantity(self):
        db = FakeDB()
        db.state["matches"][0]["product_quantity"] = 1
        self.assertEqual(offers.matches(db.cursor(), 774, "in.optiwar.com", 2), [])
        self.assertEqual(db.cursor_obj.queries[-1][1], (774, 2))


class OfferTransactionTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.now = datetime(2025, 1, 1)

    def _create(self, reason="QC_FAILED"):
        return offers.create_offer(
            self.db, "O-1", 11, reason, [20], "operator", self.now,
            token_secret="test-secret")

    def test_qc_offer_creates_guard_and_history_atomically(self):
        row, token, link = self._create()
        self.assertEqual(row["status"], "OFFERED")
        self.assertTrue(token)
        self.assertEqual(link, "https://in.optiwar.com/fq/" + token)
        self.assertEqual(self.db.state["guard"],
                         (11, "O-1", 10, "operator"))
        self.assertIn("Frame AB28 failed quality check; replacement offered: AB27 (by operator)",
                      self.db.state["history"])
        self.assertEqual(self.db.commits, 1)
        self.assertNotIn(token, str(self.db.state["history"]))

    def test_out_of_stock_offer_does_not_create_qc_guard(self):
        self._create("OUT_OF_STOCK")
        self.assertIsNone(self.db.state["guard"])
        self.assertIn("out of stock", self.db.state["history"][0])
        self.assertNotIn("failed quality check", self.db.state["history"][0])

    def test_create_offer_rejects_stock_below_line_quantity(self):
        self.db.state["line"]["order_quantity"] = 2
        self.db.state["matches"][0]["product_quantity"] = 1
        with self.assertRaises(offers.OfferError) as caught:
            self._create()
        self.assertEqual(caught.exception.code, "candidate_changed")

    def test_swap_changes_line_stock_status_and_removes_qc_guard(self):
        self.db.state["matches"][0]["product_special_price"] = "130.00"
        row, _token, _link = self._create()
        decision = offers.decide(self.db, row, product_id=20, now=self.now)
        self.assertEqual(decision["settlement"], "WAIVED")
        self.assertEqual(offers.price_label(13000, 10000, 1), "No extra charge")
        self.assertEqual(self.db.state["line"]["product_id"], 20)
        self.assertEqual(self.db.state["product"]["product_quantity"], 0)
        self.assertEqual(self.db.state["product"]["product_status"], "OUT_OF_STOCK")
        self.assertIsNone(self.db.state["guard"])
        self.assertEqual(self.db.state["offer"]["status"], "SWAPPED")
        self.assertEqual(len(self.db.state["status_history"]), 1)

    def test_quantity_two_reserves_two_and_doubles_the_difference(self):
        self.db.state["line"]["order_quantity"] = 2
        self.db.state["product"]["product_quantity"] = 2
        self.db.state["matches"][0]["product_quantity"] = 2
        self.db.state["matches"][0]["product_special_price"] = "130.00"
        row, _token, _link = self._create()
        result = offers.decide(self.db, row, product_id=20, now=self.now)
        self.assertEqual(self.db.state["product"]["product_quantity"], 0)
        self.assertEqual(result["diff_minor"], 6000)
        self.assertEqual(offers.price_label(13000, 10000, 2), "Pay ₹60 more")
        self.assertEqual(self.db.state["offer"]["price_diff_minor"], 6000)
        self.assertEqual(self.db.state["offer"]["settlement"], "PAYMENT_DUE")

    def test_quantity_two_stock_race_rolls_back_when_only_one_remains(self):
        self.db.state["line"]["order_quantity"] = 2
        self.db.state["product"]["product_quantity"] = 2
        self.db.state["matches"][0]["product_quantity"] = 2
        row, _token, _link = self._create()
        history_count = len(self.db.state["history"])
        self.db.state["product"]["product_quantity"] = 1
        with self.assertRaises(offers.OfferError) as caught:
            offers.decide(self.db, row, product_id=20, now=self.now)
        self.assertEqual(caught.exception.code, "sold_out")
        self.assertEqual(self.db.state["product"]["product_quantity"], 1)
        self.assertEqual(self.db.state["line"]["product_id"], 10)
        self.assertEqual(self.db.state["offer"]["status"], "OFFERED")
        self.assertEqual(len(self.db.state["history"]), history_count)
        self.assertEqual(self.db.rollbacks, 1)

    def test_stock_race_rolls_back_without_changing_order_line(self):
        row, _token, _link = self._create()
        self.db.state["product"]["product_quantity"] = 0
        with self.assertRaises(offers.OfferError) as caught:
            offers.decide(self.db, row, product_id=20, now=self.now)
        self.assertEqual(caught.exception.code, "sold_out")
        self.assertEqual(self.db.state["line"]["product_id"], 10)
        self.assertEqual(self.db.rollbacks, 1)

    def test_price_difference_boundaries(self):
        cases = {
            200: "WAIVED",
            -4900: "WAIVED",
            -10000: "REFUND_DUE",
            5000: "PAYMENT_DUE",
        }
        for diff, settlement in cases.items():
            with self.subTest(diff=diff):
                self.assertEqual(offers.settlement_for(diff), settlement)

    def test_refund_choice_is_only_a_staff_request(self):
        row, _token, _link = self._create()
        decision = offers.decide(self.db, row, refund=True, now=self.now)
        self.assertEqual(decision["action"], "refund")
        self.assertEqual(self.db.state["offer"]["status"], "REFUND_REQUESTED")
        self.assertIn(
            "Customer asked for a full refund via EWS (frame AB28 failed QC)",
            self.db.state["history"])
        self.assertFalse(any(
            sql.lstrip().lower().startswith(
                ("insert into order_refunds", "update order_refunds",
                 "delete from order_refunds"))
            for sql, _params in self.db.cursor_obj.queries))

    def test_refund_choice_amount_is_remaining_paid_balance(self):
        self.db.state["paid_total_minor"] = 254900
        self.db.state["refunds"] = [
            {"amount_minor": 20000, "status": "PROCESSED"}]
        row, _token, _link = self._create()
        decision = offers.decide(self.db, row, refund=True, now=self.now)
        self.assertEqual(decision["amount_minor"], 234900)

    def test_delivery_fee_refund_does_not_cancel_offer(self):
        row, _token, _link = self._create()
        self.db.state["refunds"] = [
            {"amount_minor": 500, "status": "PROCESSED"}]
        self.db.state["order_status_name"] = "Partially Refunded"
        result = offers.decide(self.db, row, product_id=20, now=self.now)
        self.assertEqual(result["action"], "frame")
        self.assertEqual(self.db.state["offer"]["status"], "SWAPPED")

    def test_pending_refund_for_paid_total_cancels_offer(self):
        row, _token, _link = self._create()
        self.db.state["refunds"] = [
            {"amount_minor": 10000, "status": "PENDING"}]
        with self.assertRaises(offers.OfferError) as caught:
            offers.decide(self.db, row, product_id=20, now=self.now)
        self.assertEqual(caught.exception.code, "closed")
        self.assertEqual(self.db.state["offer"]["status"], "CANCELLED")
        self.assertEqual(self.db.state["product"]["product_quantity"], 1)
        self.assertEqual(self.db.state["line"]["product_id"], 10)

    def test_full_refund_cancels_offer_by_status_or_processed_total(self):
        for refund_choice, refunds, status in (
                (False, [], "Refunded"),
                (True, [{"amount_minor": 10000, "status": "PROCESSED"}],
                 "Partially Refunded")):
            with self.subTest(refund=refund_choice, status=status):
                db = FakeDB()
                row, _token, _link = offers.create_offer(
                    db, "O-1", 11, "QC_FAILED", [20], "operator",
                    datetime(2025, 1, 1), token_secret="test-secret")
                db.state["refunds"] = refunds
                db.state["order_status_name"] = status
                with self.assertRaises(offers.OfferError) as caught:
                    offers.decide(db, row, product_id=20, refund=refund_choice,
                                  now=datetime(2025, 1, 1))
                self.assertEqual(caught.exception.code, "closed")
                self.assertEqual(caught.exception.status, 410)
                self.assertEqual(db.state["offer"]["status"], "CANCELLED")
                self.assertEqual(db.state["product"]["product_quantity"], 1)
                self.assertEqual(db.state["line"]["product_id"], 10)
                self.assertEqual(db.commits, 2)

    def test_expired_and_used_offer_decisions_return_410(self):
        row, _token, _link = self._create()
        self.db.state["offer"]["expires_at"] = self.now - timedelta(seconds=1)
        with self.assertRaises(offers.OfferError) as caught:
            offers.decide(self.db, row, product_id=20, now=self.now)
        self.assertEqual(caught.exception.status, 410)
        self.db.state["offer"]["status"] = "SWAPPED"
        self.db.state["offer"]["expires_at"] = self.now + timedelta(days=1)
        with self.assertRaises(offers.OfferError) as caught:
            offers.decide(self.db, row, product_id=20, now=self.now)
        self.assertEqual(caught.exception.status, 410)

    def test_customer_route_returns_410_for_expired_and_used_tokens(self):
        token_db = FakeDB()
        token = "test-token"
        row = {"id": 1, "offer_uuid": "uuid", "token_hash": hashlib.sha256(token.encode()).hexdigest(),
               "status": "OFFERED", "expires_at": self.now - timedelta(seconds=1),
               "candidates_json": "[]"}
        token_db.state["offer"] = row
        db_module = types.ModuleType(PACKAGE + ".db")
        db_module.get_db = lambda: token_db
        sys.modules[db_module.__name__] = db_module
        auth_module = types.ModuleType(PACKAGE + ".order_face_scan")
        auth_module._ops_auth = lambda: None
        sys.modules[auth_module.__name__] = auth_module
        spec = importlib.util.spec_from_file_location(
            PACKAGE + ".frame_offers_api", ROOT / "frame_offers_api.py")
        api = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = api
        spec.loader.exec_module(api)
        app = Flask(__name__, template_folder=str(ROOT / "templates"))
        app.secret_key = "test"
        bp = Blueprint("main", __name__)
        api.register(bp)
        app.register_blueprint(bp)
        client = app.test_client()
        self.assertEqual(client.get("/fq/" + token).status_code, 410)
        self.assertEqual(client.get("/fq/" + token).headers["Referrer-Policy"],
                         "no-referrer")
        token_db.state["offer"]["status"] = "SWAPPED"
        token_db.state["offer"]["expires_at"] = self.now + timedelta(days=1)
        self.assertEqual(client.get("/fq/" + token).status_code, 410)

    def test_guest_refund_button_shows_remaining_refundable_amount(self):
        token_db = FakeDB()
        token = "refund-token"
        token_db.state["line"]["order_total"] = 2349
        token_db.state["delivery_fee_minor"] = 20000
        token_db.state["refunds"] = [
            {"amount_minor": 20000, "status": "PROCESSED"}]
        token_db.state["offer"] = {
            "id": 1, "offer_uuid": "refund-offer", "order_id": "O-1",
            "order_line_id": 11,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "status": "OFFERED",
            "expires_at": datetime.now() + timedelta(days=1),
            "failed_price_minor": 10000,
            "candidates_json": json.dumps([{
                "product_id": 20, "code": "AB27", "name": "Replacement",
                "price_minor": 10000, "size": "52-18", "colour": "Black",
            }]),
        }
        db_module = types.ModuleType(PACKAGE + ".db")
        db_module.get_db = lambda: token_db
        sys.modules[db_module.__name__] = db_module
        auth_module = types.ModuleType(PACKAGE + ".order_face_scan")
        auth_module._ops_auth = lambda: None
        sys.modules[auth_module.__name__] = auth_module
        spec = importlib.util.spec_from_file_location(
            PACKAGE + ".frame_offers_api", ROOT / "frame_offers_api.py")
        api = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = api
        spec.loader.exec_module(api)
        api._hits.clear()
        app = Flask(__name__, template_folder=str(ROOT / "templates"))
        app.secret_key = "test"
        bp = Blueprint("main", __name__)
        api.register(bp)
        app.register_blueprint(bp)
        response = app.test_client().get("/fq/" + token)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Cancel and refund me in full (₹2,349)",
            response.get_data(as_text=True))
        token_db.state["refunds"] = []
        api._hits.clear()
        response = app.test_client().get("/fq/" + token)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Cancel and refund me in full (₹2,549)",
            response.get_data(as_text=True))

    def test_ops_routes_require_auth_and_authorized_session_succeeds(self):
        auth = {"authorized": False}
        db = FakeDB()
        db_module = types.ModuleType(PACKAGE + ".db")
        db_module.get_db = lambda: db
        sys.modules[db_module.__name__] = db_module
        auth_module = types.ModuleType(PACKAGE + ".order_face_scan")
        auth_module._ops_auth = lambda: auth["authorized"]
        sys.modules[auth_module.__name__] = auth_module
        spec = importlib.util.spec_from_file_location(
            PACKAGE + ".frame_offers_api", ROOT / "frame_offers_api.py")
        api = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = api
        spec.loader.exec_module(api)
        app = Flask(__name__, template_folder=str(ROOT / "templates"))
        app.secret_key = "test"
        bp = Blueprint("main", __name__)
        api.register(bp)
        app.register_blueprint(bp)
        client = app.test_client()

        routes = (
            ("GET", "/ops/api/orders/O-1/frame-matches?order_line_id=11"),
            ("POST", "/ops/api/orders/O-1/frame-offer"),
            ("GET", "/ops/api/orders/O-1/frame-offer"),
            ("POST", "/ops/api/orders/O-1/frame-offer/offer-uuid/settled"),
            ("GET", "/ops/api/frame-offers/open"),
            ("GET", "/ops/api/frame-offers/offer-uuid/link"),
            ("POST", "/ops/api/frame-offers/offer-uuid/mark"),
        )
        for method, path in routes:
            with self.subTest(method=method, path=path):
                response = client.open(path, method=method, json={})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.get_json(),
                                 {"ok": False, "error": "unauthorized"})
        self.assertEqual(db.cursor_obj.queries, [])

        auth["authorized"] = True
        with patch.object(api.offers, "open_offers", return_value=[]):
            response = client.get("/ops/api/frame-offers/open")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "offers": []})

        db.state["paid_total_minor"] = 254900
        db.state["refunds"] = [
            {"amount_minor": 20000, "status": "PROCESSED"}]
        line = {"order_line_id": 11, "product_id": 10, "site_from": "in.optiwar.com",
                "order_quantity": 1, "product_special_price": "100.00"}
        with (patch.object(api.offers, "_line", return_value=line),
              patch.object(api.offers, "matches", return_value=[]),
              patch.object(api.offers, "_candidate_rows", return_value=[])):
            response = client.get(
                "/ops/api/orders/O-1/frame-matches?order_line_id=11")
        self.assertEqual(response.get_json()["refund_balance_minor"], 234900)

        created = {"offer_uuid": "offer-1", "order_line_id": 11,
                   "order_quantity": 1, "expires_at": "2026-01-01",
                   "candidates": []}
        with patch.object(api.offers, "create_offer",
                          return_value=(created, None, "https://example.test/fq/token")):
            response = client.post("/ops/api/orders/O-1/frame-offer", json={
                "order_line_id": 11, "reason": "QC_FAILED",
                "product_ids": [20], "created_by": "Ops"})
        self.assertEqual(response.get_json()["refund_balance_minor"], 234900)

        with patch.object(api.offers, "latest_for_order",
                          return_value=[dict(created, status="OFFERED")]):
            response = client.get(
                "/ops/api/orders/O-1/frame-offer?order_line_id=11")
        self.assertEqual(response.get_json()["refund_balance_minor"], 234900)
        self.assertEqual(
            response.get_json()["offers"][0]["refund_balance_minor"], 234900)


class ReminderLinkTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2025, 1, 1)
        self.offer_uuid = "offer-uuid"
        self.secret = "test-secret"
        token = offers.token_for(self.offer_uuid, self.secret)
        self.row = {
            "offer_uuid": self.offer_uuid,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "status": "OFFERED",
            "expires_at": self.now + timedelta(days=1),
            "site_from": "in.optiwar.com",
        }

    def test_link_is_rederived_for_an_open_unexpired_offer(self):
        token = offers.token_for(self.offer_uuid, self.secret)
        self.assertEqual(offers.link_for(self.row, self.secret, self.now),
                         "https://in.optiwar.com/fq/" + token)

    def test_link_refuses_closed_offer(self):
        self.row["status"] = "CANCELLED"
        with self.assertRaises(offers.OfferError) as caught:
            offers.link_for(self.row, self.secret, self.now)
        self.assertEqual(caught.exception.status, 404)

    def test_link_refuses_expired_offer(self):
        self.row["expires_at"] = self.now
        with self.assertRaises(offers.OfferError) as caught:
            offers.link_for(self.row, self.secret, self.now)
        self.assertEqual(caught.exception.status, 410)


class RestockGuardTests(unittest.TestCase):
    def test_existing_qc_guard_prevents_refund_restock(self):
        sys.path.insert(0, str(ROOT))
        import refunds
        refunds._AWB_TABLE_EXISTS = None

        class Cursor:
            def __init__(self):
                self.rowcount = 0
                self.one = None
                self.rows = []
                self.updates = []

            def execute(self, sql, params=()):
                query = " ".join(sql.lower().split())
                if query.startswith("select max(is_test)"):
                    self.one = {"is_test": 0}
                elif query.startswith("select 1 from order_status"):
                    self.one = None
                elif query.startswith("show tables like"):
                    self.one = {"table": "ops_shipping_awb"}
                elif query.startswith("select 1 from ops_shipping_awb"):
                    self.one = None
                elif query.startswith("select o.order_line_id"):
                    self.rows = [{"order_line_id": 11, "product_id": 10,
                                  "qty": 1, "product_code": "AB28"}]
                elif query.startswith("insert ignore into stock_restocks"):
                    self.rowcount = 0
                elif query.startswith("update products"):
                    self.updates.append(sql)
                    self.rowcount = 1
                else:
                    raise AssertionError("unexpected SQL: %s" % sql)

            def fetchone(self):
                return self.one

            def fetchall(self):
                return self.rows

        cursor = Cursor()
        restored = refunds.restock_unshipped(
            cursor, "O-1", 7, "operator", reason="refund")
        self.assertEqual(restored, [])
        self.assertEqual(cursor.updates, [])


if __name__ == "__main__":
    unittest.main()
