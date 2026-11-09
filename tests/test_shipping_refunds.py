"""Possible refunds (shipping_refunds.py): which paid delivery options missed
their promise, the count Ops sees blinking, and the one-click refund of the fee
through the refund engine. No database; the engine and provider are faked."""
import datetime
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
D = datetime.date
DT = datetime.datetime


def _plain():
    spec = importlib.util.spec_from_file_location("shipping_refunds_plain", os.path.join(REPO, "shipping_refunds.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def row(order_id="A1", option="ncr24", fee=1000, promised=D(2026, 11, 8), delivered=None, shipped=None, status="Shipped"):
    return {"order_id": order_id, "option_key": option, "fee": fee, "promised_date": promised, "state": "Delhi",
            "zipcode": "110001", "delivered_at": delivered, "shipped_at": shipped, "latest_status": status,
            "payment_date": DT(2026, 11, 7, 10, 0)}


class _Cursor:
    """Answers the candidates query, then the ledger query."""
    def __init__(self, rows, ledger=(), fail=False):
        self.rows, self.ledger, self.fail, self.calls, self._out = list(rows), list(ledger), fail, [], []

    def execute(self, sql, args=None):
        if self.fail:
            raise RuntimeError("no table")
        self.calls.append((sql, args))
        self._out = self.ledger if "FROM order_refunds" in sql else self.rows

    def fetchall(self):
        return self._out


class Rules(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.sr = _plain()

    def test_late_is_delivered_after_the_promise_or_not_delivered_once_it_passed(self):
        today = D(2026, 11, 10)
        self.assertEqual(self.sr.lateness(row(delivered=DT(2026, 11, 9, 14, 0)), today), (1, "delivered late"))
        self.assertEqual(self.sr.lateness(row(), today), (2, "not delivered yet"))
        self.assertEqual(self.sr.lateness(row(delivered=DT(2026, 11, 8, 23, 0)), today), (0, ""))     # on the day: kept
        self.assertEqual(self.sr.lateness(row(promised=D(2026, 11, 10)), today), (0, ""))            # not passed yet
        self.assertEqual(self.sr.lateness(row(promised=None), today), (0, ""))

    def test_the_list_keeps_only_missed_promises_and_counts_the_open_ones(self):
        rows = [row("A1"), row("B2", delivered=DT(2026, 11, 8, 12, 0)), row("C3", option="expedite", fee=300,
                                                                           delivered=DT(2026, 11, 11, 9, 0))]
        ledger = [{"order_id": "C3", "idempotency_key": "shipfee-C3", "status": "PROCESSED", "amount_minor": 30000,
                   "requested_by": "ops@optiwar.com", "requested_at": DT(2026, 11, 11, 12, 0), "completed_at": None,
                   "provider_refund_id": "rfnd_1", "error_text": None}]
        cur = _Cursor(rows, ledger)
        out = self.sr.candidates(cur, today=D(2026, 11, 12))
        self.assertEqual([r["order_id"] for r in out], ["A1", "C3"])
        self.assertEqual((out[0]["days_late"], out[0]["basis"], out[0]["fee_minor"], out[0]["refunded"]), (4, "not delivered yet", 100000, False))
        self.assertEqual((out[1]["days_late"], out[1]["basis"], out[1]["refunded"]), (3, "delivered late", True))
        self.assertEqual(out[1]["refund"]["requested_by"], "ops@optiwar.com")
        self.assertEqual(self.sr.open_count(out), 1)
        sql, args = cur.calls[0]
        self.assertIn("s.fee > 0 AND s.promised_date < %s", sql)
        self.assertIn("status = 'TXN_SUCCESS'", sql)             # paid orders only
        self.assertIn("NOT LIKE '%%TEST_BUY%%'", sql)            # never a test order
        self.assertEqual(args, (D(2026, 11, 12),))
        self.assertEqual(self.sr.candidates(_Cursor([], fail=True)), [])

    def test_a_failed_attempt_can_be_retried_and_a_done_one_cannot_be_replayed(self):
        failed = {"order_id": "A1", "idempotency_key": "shipfee-A1", "status": "FAILED", "requested_by": "x"}
        self.assertEqual(self.sr.next_key("A1", []), "shipfee-A1")
        self.assertEqual(self.sr.next_key("A1", [failed]), "shipfee-A1-r2")
        self.assertEqual(self.sr.refund_state([failed])["status"], "FAILED")
        done = dict(failed, status="PENDING", idempotency_key="shipfee-A1-r2")
        self.assertEqual(self.sr.refund_state([failed, done])["status"], "PENDING")
        out = self.sr.candidates(_Cursor([row("A1")], [failed]), today=D(2026, 11, 12))
        self.assertFalse(out[0]["refunded"])
        self.assertEqual(self.sr.open_count(out), 1)

    def test_the_comment_and_json_say_what_happened(self):
        out = self.sr.candidates(_Cursor([row("A1")]), today=D(2026, 11, 10))[0]
        self.assertEqual(self.sr.comment_for(out), "Delivery promise missed: 24-hour delivery (Delhi NCR) fee Rs 1000, "
                         "promised by 08 Nov 2026, not delivered yet (2 days late). Fee refunded to source by Ops.")
        j = self.sr.as_json(out)
        self.assertEqual((j["order_id"], j["fee_minor"], j["currency"], j["days_late"], j["refunded"], j["refund"]),
                         ("A1", 100000, "INR", 2, False, None))
        self.assertEqual(j["promised_date"], "2026-11-08")


class Routes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Blueprint, Flask
        pkg_name = "shr_pkg"
        pkg = types.ModuleType(pkg_name); pkg.__path__ = [REPO]; sys.modules[pkg_name] = pkg
        cls.auth, cls.rows, cls.ledger, cls.executed = [True], [], [], []

        class DB:
            def cursor(self):
                return _Cursor(cls.rows, cls.ledger)
        db = types.ModuleType(pkg_name + ".db"); db.get_db = lambda: DB(); sys.modules[db.__name__] = db; pkg.db = db
        ops = types.ModuleType(pkg_name + ".ops"); ops._require_ops_auth = lambda: cls.auth[0]
        sys.modules[ops.__name__] = ops; pkg.ops = ops

        class RefundRejected(Exception):
            def __init__(self, code, message, http_status=422):
                super().__init__(message); self.code, self.message, self.http_status = code, message, http_status

        class ProviderError(Exception):
            pass
        refunds = types.ModuleType(pkg_name + ".refunds")
        refunds.FAILED = "FAILED"; refunds.RefundRejected = RefundRejected
        refunds.ensure_schema = lambda cursor: None
        cls.outcome = ["PROCESSED"]

        def execute(db, order_id, **kw):
            if cls.outcome[0] == "REJECT":
                raise RefundRejected("amount_exceeds_refundable", "at most 0 minor units remain refundable")
            cls.executed.append((order_id, kw))
            return {"status": cls.outcome[0], "provider_refund_id": "rfnd_TEST", "error_text": None}
        refunds.execute = execute
        sys.modules[refunds.__name__] = refunds; pkg.refunds = refunds
        ops_refunds = types.ModuleType(pkg_name + ".ops_refunds")
        ops_refunds.ProviderError = ProviderError; ops_refunds._provider = lambda: "PROVIDER"
        sys.modules[ops_refunds.__name__] = ops_refunds; pkg.ops_refunds = ops_refunds
        spec = importlib.util.spec_from_file_location(pkg_name + ".shipping_refunds", os.path.join(REPO, "shipping_refunds.py"))
        mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)
        cls.sr = mod
        cls.tmp = tempfile.mkdtemp(prefix="shr")
        shutil.copy(os.path.join(REPO, "templates", "ops_possible_refunds.html"), cls.tmp)
        cls.app = Flask(__name__, template_folder=cls.tmp)
        cls.app.config.update(TESTING=True, SECRET_KEY="t")
        bp = Blueprint("main", __name__)
        mod.register(bp)
        cls.app.register_blueprint(bp)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.auth[0] = True
        self.outcome[0] = "PROCESSED"
        del self.executed[:]
        self.rows[:] = [row("A1", promised=D(2020, 1, 1)), row("C3", option="expedite", fee=300, promised=D(2020, 1, 1))]
        self.ledger[:] = []

    def test_all_three_routes_fail_closed(self):
        self.auth[0] = False
        self.assertEqual(self.client.get("/ops/possible-refunds").status_code, 401)
        self.assertEqual(self.client.get("/api/ops/shipping/possible-refunds").status_code, 401)
        self.assertEqual(self.client.post("/api/ops/shipping/possible-refunds/A1/refund").status_code, 401)
        self.assertEqual(self.executed, [])

    def test_the_list_and_the_blinking_count(self):
        j = self.client.get("/api/ops/shipping/possible-refunds").get_json()
        self.assertEqual((j["open"], j["count"]), (2, 2))
        page = self.client.get("/ops/possible-refunds")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(b'class="badge on"', page.data)
        self.assertIn(b"2 open", page.data)
        self.assertIn(b"Refund Rs 1000 to source", page.data)
        self.rows[:] = []
        page = self.client.get("/ops/possible-refunds")
        self.assertNotIn(b'class="badge on"', page.data)
        self.assertIn(b"Nothing to refund", page.data)

    def test_a_click_refunds_exactly_the_stored_fee_once_with_the_person_named(self):
        r = self.client.post("/api/ops/shipping/possible-refunds/A1/refund", json={"amount_minor": 999999})
        self.assertEqual(r.status_code, 400)                       # no person named
        self.assertEqual(self.executed, [])
        r = self.client.post("/api/ops/shipping/possible-refunds/A1/refund", headers={"X-Ops-Operator": "Asha (Ops)"},
                             json={"amount_minor": 999999})
        self.assertEqual(r.status_code, 200, r.data)
        order_id, kw = self.executed[0]
        self.assertEqual(order_id, "A1")
        self.assertEqual((kw["amount_minor"], kw["currency"], kw["reason_code"]), (100000, "INR", "GOODWILL"))   # never the posted amount
        self.assertEqual((kw["idempotency_key"], kw["requested_by"], kw["service_identity"]), ("shipfee-A1", "Asha (Ops)", "ops-console"))
        self.assertEqual(kw["provider"], "PROVIDER")
        self.assertIn("24-hour delivery (Delhi NCR) fee Rs 1000", kw["comment"])
        self.assertIsNone(kw["requested_status"])
        # once the ledger holds it, the button is gone and a second click is refused
        self.ledger[:] = [{"order_id": "A1", "idempotency_key": "shipfee-A1", "status": "PROCESSED", "amount_minor": 100000,
                           "requested_by": "Asha (Ops)", "requested_at": DT(2026, 11, 12, 10, 0), "completed_at": None,
                           "provider_refund_id": "rfnd_TEST", "error_text": None}]
        r = self.client.post("/api/ops/shipping/possible-refunds/A1/refund", headers={"X-Ops-Operator": "Asha (Ops)"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.executed), 1)
        page = self.client.get("/ops/possible-refunds")
        self.assertIn(b"Refunded Rs 1000", page.data)
        self.assertIn(b"by Asha (Ops)", page.data)

    def test_only_a_missed_promise_can_be_refunded_and_engine_refusals_are_passed_on(self):
        r = self.client.post("/api/ops/shipping/possible-refunds/ZZ9/refund", headers={"X-Ops-Operator": "Asha"})
        self.assertEqual(r.status_code, 404)
        self.outcome[0] = "REJECT"
        r = self.client.post("/api/ops/shipping/possible-refunds/A1/refund", headers={"X-Ops-Operator": "Asha"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()["error"], "amount_exceeds_refundable")
        self.outcome[0] = "FAILED"
        r = self.client.post("/api/ops/shipping/possible-refunds/C3/refund", headers={"X-Ops-Operator": "Asha"})
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.get_json()["ok"])


class Wiring(unittest.TestCase):

    def test_registered_deployed_and_never_automatic(self):
        models = open(os.path.join(REPO, "models.py"), encoding="utf-8").read()
        self.assertIn("shipping_refunds.register(bp)", models)
        deploy = open(os.path.join(REPO, "deploy", "deploy.py"), encoding="utf-8").read()
        dep_block = deploy[deploy.index("DEPLOY_SET = ("):deploy.index("NEW_IN_RELEASE = (")]
        new_block = deploy[deploy.index("NEW_IN_RELEASE = ("):]
        new_block = new_block[:new_block.index('")\n') + 2]
        for name in ('"shipping_refunds.py"', '"templates/ops_possible_refunds.html"'):
            self.assertIn(name, dep_block)
            self.assertIn(name, new_block)
        src = open(os.path.join(REPO, "shipping_refunds.py"), encoding="utf-8").read()
        # the only call into the refund engine is inside the POST handler a person triggers
        self.assertEqual(src.count("refunds.execute("), 1)
        self.assertIn('methods=["POST"])\n    def ops_shipping_refund_fee(order_id):', src)
        self.assertIn("amount_minor=row[\"fee_minor\"]", src)


if __name__ == "__main__":
    unittest.main()
