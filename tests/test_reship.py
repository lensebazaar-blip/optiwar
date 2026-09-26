"""India Return-to-Origin / reship: the ledger, the fee payment, the Ops
boundary and the My Orders card, against a real test database.

Courier ``Returned`` is only RETURNING_TO_OPS; a named operator makes it
RETURNED_TO_OPS; the customer pays exactly INR 250 on a dedicated Razorpay
order; browser callback, webhook and reconciliation converge on one PAID row;
Ops ships under a new AWB and the original AWB is never rewritten.
"""
import importlib.util
import os
import sys
import time
import types
import unittest
import uuid

from flask import Blueprint, Flask

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.test_paid_order_pipeline import DDL, _connect  # noqa: E402
from tests.test_razorpay_settlement import EXTRA_DDL  # noqa: E402

PKG = "flaskr_reship_t"
TOKEN = "ops-token-for-tests"
IN = {"HTTP_HOST": "optiwar.in"}
COM = {"HTTP_HOST": "optiwar.com"}

AWB_DDL = """CREATE TABLE IF NOT EXISTS ops_shipping_awb (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ow_order_id VARCHAR(64) NOT NULL,
    tracking_number VARCHAR(64) NULL,
    courier VARCHAR(64) NULL,
    awb_status VARCHAR(32) NULL,
    label_pdf_path VARCHAR(255) NULL,
    courier_response TEXT NULL,
    created_by VARCHAR(100) NULL,
    created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NULL,
    KEY idx_order (ow_order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


class Stubs:
    """What the routes reach outside the ledger: the provider and the
    notification layer. Every call is recorded."""
    db = None
    provider_orders = []
    provider_payments = {}
    signature_ok = True
    mails = []
    shipped = []


def _load(name):
    spec = importlib.util.spec_from_file_location(
        "%s.%s" % (PKG, name), os.path.join(REPO, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _package():
    if PKG in sys.modules:
        return
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [REPO]
    sys.modules[PKG] = pkg

    db = types.ModuleType(PKG + ".db")
    db.get_db = lambda: Stubs.db
    sys.modules[db.__name__] = db

    notes = types.ModuleType(PKG + ".notifications")

    def notify_order_shipped(email, phone, name, order_id, host, tracking_info='', **kw):
        Stubs.shipped.append((order_id, tracking_info))
        return True
    notes.notify_order_shipped = notify_order_shipped
    notes.notify_support_ticket_resolved = lambda *a, **k: None
    notes.send_whatsapp_tracked = lambda *a, **k: {"ok": True}
    sys.modules[notes.__name__] = notes

    pay = types.ModuleType(PKG + ".payments")

    def create_reship_razorpay_order(amount, currency, receipt, notes_):
        oid = "order_RS%06d" % (len(Stubs.provider_orders) + 1)
        rec = {"id": oid, "amount": amount, "currency": currency, "receipt": receipt,
               "notes": dict(notes_), "status": "created"}
        Stubs.provider_orders.append(rec)
        return rec
    pay.create_reship_razorpay_order = create_reship_razorpay_order
    pay.verify_razorpay_payment = lambda oid, pid, sig: Stubs.signature_ok
    pay.fetch_razorpay_payment = lambda pid: Stubs.provider_payments.get(pid)
    sys.modules[pay.__name__] = pay

    _load("paid_orders")
    _load("ops")
    _load("reship")
    _load("reship_api")
    _load("customer_orders")


_package()
reship = sys.modules[PKG + ".reship"]
reship_api = sys.modules[PKG + ".reship_api"]
attach_reship = sys.modules[PKG + ".customer_orders"].attach_reship


def _app():
    app = Flask(__name__, template_folder=os.path.join(REPO, "templates"))
    app.secret_key = "test-only"
    app.config["OPS_API_TOKEN"] = TOKEN
    app.config["RAZORPAY_KEY_ID"] = "rzp_test_x"
    bp = Blueprint("main", __name__)
    reship_api.register(bp)
    app.register_blueprint(bp)
    return app


def _payment(pid, rzp_order, amount=25000, currency="INR", status="captured", notes=None):
    return {"id": pid, "entity": "payment", "order_id": rzp_order, "amount": amount,
            "currency": currency, "status": status, "method": "upi",
            "notes": notes if notes is not None else {}, "created_at": int(time.time())}


class ReshipTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = _connect()
        if cls.db is None:
            raise unittest.SkipTest("no test database reachable")
        cur = cls.db.cursor()
        for stmt in list(DDL) + list(EXTRA_DDL) + [AWB_DDL]:
            cur.execute(stmt)
        cls.db.commit()
        reship._SCHEMA_READY = False
        reship.ensure_schema(cls.db)
        Stubs.db = cls.db

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "db", None) is not None:
            cls.db.close()

    def setUp(self):
        os.environ[reship.ENABLED_ENV] = "true"
        os.environ.pop(reship.ALLOW_ORDERS_ENV, None)
        os.environ.pop(reship.WA_APPROVED_ENV, None)
        Stubs.provider_orders = []
        Stubs.provider_payments = {}
        Stubs.signature_ok = True
        Stubs.mails = []
        Stubs.shipped = []
        self._orders = []
        self._customers = []
        self.cur = self.db.cursor()
        self.db.commit()
        self._mail = reship._default_mailer
        reship._default_mailer = lambda to, subj, text: Stubs.mails.append((to, subj))

    def tearDown(self):
        reship._default_mailer = self._mail
        self.db.rollback()
        cur = self.db.cursor()
        for oid in self._orders:
            for table, col in (("orders", "order_id"), ("order_status", "order_id"),
                               ("order_history", "order_id"), ("payment_collector", "order_id"),
                               ("order_reshipments", "order_id"), ("reship_events", "order_id"),
                               ("ops_shipping_awb", "ow_order_id")):
                cur.execute("DELETE FROM %s WHERE %s=%%s" % (table, col), (oid,))
        for cid in self._customers:
            cur.execute("DELETE FROM customers WHERE customer_id=%s", (cid,))
        self.db.commit()

    # ---------------------------------------------------------------- fixtures

    def _customer(self, email="c@example.in"):
        self.cur.execute("INSERT INTO customers (customer_name, customer_email, customer_phone) "
                         "VALUES ('Test Customer', %s, '919999900000')", (email,))
        cid = self.cur.lastrowid
        self._customers.append(cid)
        self.db.commit()
        return cid

    def _order(self, customer_id, site="optiwar.in", status="Returned", awb="7X119057819",
               courier="DTDC", lines=1):
        oid = "RS%s" % uuid.uuid4().hex[:10].upper()
        self._orders.append(oid)
        for _ in range(lines):
            self.cur.execute("INSERT INTO orders (order_id, customer_id, product_id, order_total, "
                             "site_from) VALUES (%s,%s,1,49900,%s)", (oid, customer_id, site))
        for st in ("Processed", "Shipped", status):
            self.cur.execute("INSERT INTO order_status (order_status_name, order_id, source, "
                             "created_at) VALUES (%s,%s,'courier',NOW())", (st, oid))
        if awb:
            self.cur.execute("INSERT INTO ops_shipping_awb (ow_order_id, tracking_number, courier, "
                             "awb_status, created_by) VALUES (%s,%s,%s,'created','ops')",
                             (oid, awb, courier))
        self.db.commit()
        return oid

    def _returned(self, customer_id=None, **kw):
        cid = customer_id or self._customer()
        oid = self._order(cid, **kw)
        row = reship.confirm_returned(self.db, oid, "ops@optiwar.com", return_reason="RTO")
        return cid, oid, row

    def _paid(self, **kw):
        cid, oid, row = self._returned(**kw)
        row, _ = reship.begin_payment(self.db, cid, row["reship_uuid"], "optiwar.in",
                                      sys.modules[PKG + ".payments"].create_reship_razorpay_order)
        pay = _payment("pay_" + uuid.uuid4().hex[:12], row["razorpay_order_id"])
        res = reship.settle_payment(self.db, row["reship_uuid"], pay, "test")
        self.assertEqual(res["outcome"], reship.APPLIED)
        return cid, oid, res["row"], pay

    def _client(self, customer_id=None, ops=False, email=None):
        """A signed-in browser: the session cookie is set for both hosts, so a
        request to .com carries the same login as one to .in."""
        app = _app()
        c = app.test_client()
        data = {}
        if customer_id is not None:
            data["user_id"] = customer_id
        if email:
            data["user_email"] = email
        if data:
            with app.test_request_context():
                value = app.session_interface.get_signing_serializer(app).dumps(dict(data))
            for host in ("optiwar.in", "optiwar.com"):
                c.set_cookie("session", value, domain=host)
        if ops:
            c.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + TOKEN
        return c

    def _events(self, oid, kind):
        return [e for e in reship.events_for(self.db, oid) if e["event_type"] == kind]

    def _awbs(self, oid):
        self.cur.execute("SELECT tracking_number, courier FROM ops_shipping_awb "
                         "WHERE ow_order_id=%s ORDER BY id", (oid,))
        return [(r["tracking_number"], r["courier"]) for r in self.cur.fetchall()]

    # ------------------------------------------------------- states and gating

    def test_courier_returned_is_only_returning_and_offers_no_payment(self):
        cid = self._customer()
        oid = self._order(cid)
        c = self._client(cid)
        r = c.get("/api/orders/%s/reship" % oid, environ_overrides=IN).get_json()
        self.assertEqual(r["reship"]["state"], "RETURNING_TO_OPS")
        self.assertFalse(r["reship"]["can_pay"])
        self.assertIsNone(r["reship"]["reship_uuid"])

    def test_ops_confirmation_makes_it_returned_and_payable(self):
        cid, oid, row = self._returned()
        self.assertEqual(row["status"], reship.ST_RETURNED)
        self.assertEqual(row["ops_return_confirmed_by"], "ops@optiwar.com")
        self.assertIsNotNone(row["ops_return_confirmed_at"])
        self.assertEqual((row["original_awb"], row["original_courier"]), ("7X119057819", "DTDC"))
        self.assertEqual(row["return_reason"], "RTO")
        r = self._client(cid).get("/api/orders/%s/reship" % oid, environ_overrides=IN).get_json()
        self.assertEqual(r["reship"]["state"], "RETURNED_TO_OPS")
        self.assertTrue(r["reship"]["can_pay"])
        self.assertEqual(r["reship"]["fee"], 250)

    def test_ops_cannot_confirm_a_parcel_the_courier_is_not_returning(self):
        cid = self._customer()
        oid = self._order(cid, status="Delivered")
        with self.assertRaises(reship.ReshipError) as ctx:
            reship.confirm_returned(self.db, oid, "ops@optiwar.com")
        self.assertEqual(ctx.exception.code, "not_returning")

    def test_dot_com_has_no_reship_workflow(self):
        cid = self._customer()
        oid = self._order(cid, site="optiwar.com")
        c = self._client(cid)
        self.assertEqual(c.get("/api/orders/%s/reship" % oid, environ_overrides=COM).status_code, 404)
        self.assertEqual(c.get("/api/orders/%s/reship" % oid, environ_overrides=IN).status_code, 404)
        with self.assertRaises(reship.ReshipError) as ctx:
            reship.confirm_returned(self.db, oid, "ops@optiwar.com")
        self.assertEqual(ctx.exception.code, "not_india")
        self.assertFalse(reship.workflow_open("optiwar.com", "optiwar.com", oid))

    def test_an_india_order_seen_from_dot_com_is_not_found_either(self):
        cid, oid, row = self._returned()
        c = self._client(cid)
        self.assertEqual(c.get("/api/orders/%s/reship" % oid, environ_overrides=COM).status_code, 404)
        self.assertEqual(c.post("/api/reshipments/%s/payment/create" % row["reship_uuid"],
                                environ_overrides=COM).status_code, 404)

    def test_flag_off_or_allow_list_closes_the_workflow(self):
        cid, oid, row = self._returned()
        os.environ[reship.ENABLED_ENV] = "false"
        self.assertEqual(self._client(cid).get("/api/orders/%s/reship" % oid,
                                               environ_overrides=IN).status_code, 404)
        os.environ[reship.ENABLED_ENV] = "true"
        os.environ[reship.ALLOW_ORDERS_ENV] = "DUNGQO-731870"
        self.assertEqual(self._client(cid).get("/api/orders/%s/reship" % oid,
                                               environ_overrides=IN).status_code, 404)
        os.environ[reship.ALLOW_ORDERS_ENV] = "DUNGQO-731870," + oid
        self.assertEqual(self._client(cid).get("/api/orders/%s/reship" % oid,
                                               environ_overrides=IN).status_code, 200)

    def test_allow_list_also_gates_the_ops_queue_and_confirmation(self):
        cid = self._customer()
        pilot = self._order(cid, awb="7X000PILOT01")
        other = self._order(cid, awb="7X119057819")
        os.environ[reship.ALLOW_ORDERS_ENV] = pilot
        q = reship.ops_queue(self.db)
        self.assertEqual([r["order_id"] for r in q["returning"]], [pilot])
        self.assertEqual(q["returning"][0]["track_url"], "https://www.dtdc.com/track")
        ops = self._client(ops=True)
        r = ops.post("/ops/api/shipments/%s/return-received" % other, environ_overrides=IN)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"], "not_in_rollout")
        self.assertIsNone(reship.active_for_order(self.db, other))
        self.assertEqual(Stubs.mails, [])
        page = ops.get("/ops/reship", environ_overrides=IN).get_data(as_text=True)
        self.assertIn(pilot, page)
        self.assertNotIn(other, page)
        self.assertEqual(ops.post("/ops/api/shipments/%s/return-received" % pilot,
                                  environ_overrides=IN).status_code, 200)

    def test_returning_card_names_the_original_shipment_and_where_to_track_it(self):
        cid = self._customer()
        oid = self._order(cid, awb="7X000PILOT01")
        view = self._client(cid).get("/api/orders/%s/reship" % oid,
                                     environ_overrides=IN).get_json()["reship"]
        self.assertEqual(view["state"], "RETURNING_TO_OPS")
        self.assertEqual((view["original_awb"], view["original_courier"]), ("7X000PILOT01", "DTDC"))
        self.assertEqual(view["original_track_url"], "https://www.dtdc.com/track")
        self.assertIsNone(view["new_track_url"])
        self.assertNotIn("razorpay_order_id", view)
        self.assertEqual(reship.tracking_url("Delhivery", "1238 6210"),
                         "https://www.delhivery.com/track-v2/package/1238%206210")
        self.assertIsNone(reship.tracking_url("BlueDart", "X1"))
        self.assertIsNone(reship.tracking_url("DTDC", ""))
        shipments = reship.shipments_for_orders(self.db, [oid, "nope"])
        self.assertEqual(shipments, {oid: ("7X000PILOT01", "DTDC")})
        orders = [{"order_id": oid, "site_from": "optiwar.in", "order_status_name": "Returned",
                   "stage_label": "", "stage_tone": "", "stage_step": 0}]
        attach_reship(orders, {}, "optiwar.in", shipments=shipments)
        self.assertEqual(orders[0]["reship"]["original_awb"], "7X000PILOT01")
        self.assertEqual(orders[0]["stage_label"], "Returning to Optiwar")
        attach_reship(orders, {}, "optiwar.com", shipments=shipments)
        self.assertIsNone(orders[0]["reship"])

    def test_another_customer_gets_404_everywhere(self):
        cid, oid, row = self._returned()
        other = self._client(self._customer("other@example.in"))
        self.assertEqual(other.get("/api/orders/%s/reship" % oid, environ_overrides=IN).status_code, 404)
        self.assertEqual(other.post("/api/reshipments/%s/payment/create" % row["reship_uuid"],
                                    environ_overrides=IN).status_code, 404)
        self.assertEqual(other.post("/api/reshipments/%s/payment/verify" % row["reship_uuid"],
                                    json={"razorpay_payment_id": "x", "razorpay_order_id": "y",
                                          "razorpay_signature": "z"},
                                    environ_overrides=IN).status_code, 404)
        anon = self._client()
        self.assertEqual(anon.get("/api/orders/%s/reship" % oid, environ_overrides=IN).status_code, 401)

    # ------------------------------------------------------------- the payment

    def test_fee_is_server_side_25000_inr_whatever_the_browser_sends(self):
        cid, oid, row = self._returned()
        c = self._client(cid)
        r = c.post("/api/reshipments/%s/payment/create" % row["reship_uuid"],
                   json={"fee": 1, "amount": 100, "currency": "USD", "customer_id": 999,
                         "status": "PAID"}, environ_overrides=IN)
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertEqual((body["amount"], body["currency"]), (25000, "INR"))
        self.assertEqual(len(Stubs.provider_orders), 1)
        po = Stubs.provider_orders[0]
        self.assertEqual((po["amount"], po["currency"]), (25000, "INR"))
        self.assertEqual(po["notes"]["purpose"], "RESHIPMENT")
        self.assertEqual(po["notes"]["reship_uuid"], row["reship_uuid"])
        self.assertEqual(po["notes"]["original_order_id"], oid)
        self.assertEqual((po["notes"]["amount"], po["notes"]["currency"]), ("25000", "INR"))
        self.assertTrue(po["receipt"].startswith("RESHIP-"))
        after = reship.by_uuid(self.db, row["reship_uuid"])
        self.assertEqual((after["status"], after["payment_status"], after["customer_id"]),
                         ("PAYMENT_PENDING", "PENDING", cid))
        self.cur.execute("SELECT order_total FROM orders WHERE order_id=%s", (oid,))
        self.assertEqual(self.cur.fetchone()["order_total"], 49900)

    def test_a_second_click_reuses_the_one_razorpay_order(self):
        cid, oid, row = self._returned()
        c = self._client(cid)
        path = "/api/reshipments/%s/payment/create" % row["reship_uuid"]
        first = c.post(path, environ_overrides=IN).get_json()
        second = c.post(path, environ_overrides=IN).get_json()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["razorpay_order_id"], second["razorpay_order_id"])
        self.assertEqual(len(Stubs.provider_orders), 1)
        self.cur.execute("SELECT COUNT(*) AS n FROM order_reshipments WHERE order_id=%s "
                         "AND status IN ('RETURNED','PAYMENT_PENDING','PAID','RESHIPPED')", (oid,))
        self.assertEqual(self.cur.fetchone()["n"], 1)
        # a second Ops confirmation is the same row too
        again = reship.confirm_returned(self.db, oid, "someone@optiwar.com")
        self.assertEqual(again["reship_uuid"], row["reship_uuid"])

    def _verify(self, c, row, pay, sig="sig"):
        return c.post("/api/reshipments/%s/payment/verify" % row["reship_uuid"],
                      json={"razorpay_payment_id": pay["id"], "razorpay_order_id": pay["order_id"],
                            "razorpay_signature": sig, "amount": 1, "status": "captured"},
                      environ_overrides=IN)

    def _start(self, cid, row):
        c = self._client(cid)
        r = c.post("/api/reshipments/%s/payment/create" % row["reship_uuid"], environ_overrides=IN).get_json()
        return c, r["razorpay_order_id"]

    def test_browser_callback_settles_from_the_fetched_payment(self):
        cid, oid, row = self._returned()
        c, rzp_order = self._start(cid, row)
        pay = _payment("pay_cb1", rzp_order)
        Stubs.provider_payments[pay["id"]] = pay
        r = self._verify(c, row, pay)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["reship"]["state"], "RESHIP_PAID")
        self.assertFalse(r.get_json()["reship"]["can_pay"])
        after = reship.by_uuid(self.db, row["reship_uuid"])
        self.assertEqual((after["status"], after["payment_status"], after["razorpay_payment_id"],
                          after["paid_source"]), ("PAID", "PAID", "pay_cb1", "browser_callback"))
        self.assertEqual(len(self._events(oid, reship.EV_PAYMENT_COMPLETED)), 1)
        self.assertEqual(len(Stubs.mails), 1)  # payment completed, once
        # the paid card no longer offers payment and a further create is refused
        self.assertEqual(c.post("/api/reshipments/%s/payment/create" % row["reship_uuid"],
                                environ_overrides=IN).status_code, 409)
        # merchandise untouched
        self.cur.execute("SELECT COUNT(*) AS n FROM payment_collector WHERE order_id=%s", (oid,))
        self.assertEqual(self.cur.fetchone()["n"], 0)

    def test_browser_claim_alone_settles_nothing(self):
        cid, oid, row = self._returned()
        c, rzp_order = self._start(cid, row)
        pay = _payment("pay_bad", rzp_order)
        Stubs.provider_payments[pay["id"]] = pay
        Stubs.signature_ok = False
        self.assertEqual(self._verify(c, row, pay).status_code, 400)
        Stubs.signature_ok = True
        Stubs.provider_payments[pay["id"]] = _payment("pay_bad", rzp_order, status="authorized")
        self.assertEqual(self._verify(c, row, pay).status_code, 202)
        after = reship.by_uuid(self.db, row["reship_uuid"])
        self.assertEqual(after["status"], "PAYMENT_PENDING")
        self.assertEqual(self._events(oid, reship.EV_PAYMENT_COMPLETED), [])

    def test_webhook_after_callback_is_a_duplicate_with_no_second_notification(self):
        cid, oid, row = self._returned()
        c, rzp_order = self._start(cid, row)
        pay = _payment("pay_dup", rzp_order)
        Stubs.provider_payments[pay["id"]] = pay
        self.assertEqual(self._verify(c, row, pay).status_code, 200)
        mails = len(Stubs.mails)
        with _app().test_request_context(base_url="https://optiwar.in/"):
            res = reship_api.settle_and_notify(self.db, row["reship_uuid"], pay,
                                               "razorpay-webhook", "optiwar.in")
        self.assertEqual(res["outcome"], reship.DUPLICATE)
        self.assertEqual(len(Stubs.mails), mails)
        self.assertEqual(len(self._events(oid, reship.EV_PAYMENT_COMPLETED)), 1)
        self.cur.execute("SELECT COUNT(*) AS n FROM order_reshipments WHERE razorpay_payment_id=%s",
                         (pay["id"],))
        self.assertEqual(self.cur.fetchone()["n"], 1)

    def test_webhook_without_callback_recovers_the_payment(self):
        cid, oid, row = self._returned()
        c, rzp_order = self._start(cid, row)
        pay = _payment("pay_wh", rzp_order, notes={})   # a webhook entity may carry no notes
        self.assertEqual(reship.reship_for_payment(self.db, pay), row["reship_uuid"])
        with _app().test_request_context(base_url="https://optiwar.in/"):
            res = reship_api.settle_and_notify(self.db, row["reship_uuid"], pay,
                                               "razorpay-webhook", "optiwar.in")
        self.assertEqual(res["outcome"], reship.APPLIED)
        after = reship.by_uuid(self.db, row["reship_uuid"])
        self.assertEqual((after["status"], after["paid_source"]), ("PAID", "razorpay-webhook"))
        # the browser arriving late is a duplicate, and the card says paid
        Stubs.provider_payments[pay["id"]] = pay
        r = self._verify(c, row, pay)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["reship"]["state"], "RESHIP_PAID")
        self.assertEqual(len(self._events(oid, reship.EV_PAYMENT_COMPLETED)), 1)

    def test_reconciliation_recovers_a_missing_callback(self):
        cid, oid, row = self._returned()
        c, rzp_order = self._start(cid, row)
        pay = _payment("pay_rec", rzp_order)
        summary = reship.reconcile_pending_payments(
            self.db, lambda o: [pay] if o == rzp_order else [])
        self.assertEqual(summary["settled"], [row["reship_uuid"]])
        self.assertEqual(reship.by_uuid(self.db, row["reship_uuid"])["status"], "PAID")
        again = reship.reconcile_pending_payments(self.db, lambda o: [pay])
        self.assertEqual(again["checked"], 0)
        # a provider that would not answer is skipped, not refused
        cid2, oid2, row2 = self._returned()
        self._start(cid2, row2)

        def down(o):
            raise RuntimeError("429")
        s = reship.reconcile_pending_payments(self.db, down)
        self.assertEqual((s["unavailable"], s["refused"]), (1, []))
        self.assertEqual(reship.by_uuid(self.db, row2["reship_uuid"])["status"], "PAYMENT_PENDING")

    def test_amount_or_currency_mismatch_never_marks_paid(self):
        cid, oid, row = self._returned()
        c, rzp_order = self._start(cid, row)
        for pay, outcome in ((_payment("pay_amt", rzp_order, amount=24900), reship.AMOUNT_MISMATCH),
                             (_payment("pay_amt2", rzp_order, amount=50000), reship.AMOUNT_MISMATCH),
                             (_payment("pay_cur", rzp_order, currency="USD"),
                              reship.CURRENCY_MISMATCH)):
            res = reship.settle_payment(self.db, row["reship_uuid"], pay, "test")
            self.assertEqual(res["outcome"], outcome)
            Stubs.provider_payments[pay["id"]] = pay
            self.assertEqual(self._verify(c, row, pay).status_code, 400)
        after = reship.by_uuid(self.db, row["reship_uuid"])
        self.assertEqual((after["status"], after["razorpay_payment_id"]), ("PAYMENT_PENDING", None))
        self.assertEqual(len(self._events(oid, reship.EV_PAYMENT_REFUSED)), 3)

    def test_a_payment_for_another_reship_or_order_is_blocked(self):
        cid, oid, row = self._returned()
        c, rzp_order = self._start(cid, row)
        cid2, oid2, row2 = self._returned()
        c2, rzp_order2 = self._start(cid2, row2)
        # paid against the other reship's Razorpay order
        other = _payment("pay_other", rzp_order2)
        res = reship.settle_payment(self.db, row["reship_uuid"], other, "test")
        self.assertEqual(res["outcome"], reship.ORDER_MISMATCH)
        Stubs.provider_payments[other["id"]] = other
        self.assertEqual(self._verify(c, row, other).status_code, 400)
        # the right order but notes naming the other reship
        named = _payment("pay_named", rzp_order, notes={"reship_uuid": row2["reship_uuid"]})
        self.assertEqual(reship.settle_payment(self.db, row["reship_uuid"], named, "test")["outcome"],
                         reship.ORDER_MISMATCH)
        # a payment id that already settled the other reship
        self.assertEqual(reship.settle_payment(self.db, row2["reship_uuid"], other, "test")["outcome"],
                         reship.APPLIED)
        self.assertEqual(reship.settle_payment(self.db, row["reship_uuid"], other, "test")["outcome"],
                         reship.ORDER_MISMATCH)
        # two reships can never hold one Razorpay order
        with self.assertRaises(Exception):
            self.cur.execute("UPDATE order_reshipments SET razorpay_order_id=%s WHERE reship_uuid=%s",
                             (rzp_order2, row["reship_uuid"]))
        self.db.rollback()
        # a payment id that already paid merchandise
        self.cur.execute("INSERT INTO payment_collector (order_id, payment_ref, payment_dump, status) "
                         "VALUES (%s,'pay_merch','{}','TXN_SUCCESS')", (oid2,))
        self.db.commit()
        merch = _payment("pay_merch", rzp_order)
        self.assertEqual(reship.settle_payment(self.db, row["reship_uuid"], merch, "test")["outcome"],
                         reship.ALREADY_BOUND)
        self.assertEqual(reship.by_uuid(self.db, row["reship_uuid"])["status"], "PAYMENT_PENDING")

    def test_merchandise_settlement_refuses_a_reship_fee_payment(self):
        rs = importlib.import_module("tests.test_razorpay_settlement").rs
        cid, oid, row = self._returned()
        c, rzp_order = self._start(cid, row)
        pay = _payment("pay_fee", rzp_order, notes={})
        self.assertTrue(rs._is_reship_fee(pay, self.db.cursor()))
        self.assertTrue(rs._is_reship_fee({"notes": {"purpose": "RESHIPMENT"}}))
        self.assertFalse(rs._is_reship_fee(_payment("pay_x", "order_unknown", notes={}),
                                           self.db.cursor()))
        out = rs.settle(self.db, oid, pay, source="test", site="optiwar.in")
        self.assertEqual(out["outcome"], rs.ALREADY_BOUND)
        self.cur.execute("SELECT COUNT(*) AS n FROM payment_collector WHERE order_id=%s", (oid,))
        self.assertEqual(self.cur.fetchone()["n"], 0)

    # ------------------------------------------------------------------- ops

    def test_ops_cannot_ship_an_unpaid_reship(self):
        cid, oid, row = self._returned()
        ops = self._client(ops=True)
        r = ops.post("/ops/api/reshipments/%s/ship" % row["reship_uuid"],
                     json={"new_awb": "NEW123", "new_courier": "DTDC"}, environ_overrides=IN)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"], "unpaid")
        self._start(cid, row)
        r = ops.post("/ops/api/reshipments/%s/ship" % row["reship_uuid"],
                     json={"new_awb": "NEW123", "new_courier": "DTDC"}, environ_overrides=IN)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self._awbs(oid), [("7X119057819", "DTDC")])
        self.assertEqual(Stubs.shipped, [])

    def test_ops_ships_after_payment_under_a_new_awb_and_the_original_stays(self):
        cid, oid, row, pay = self._paid()
        ops = self._client(ops=True)
        path = "/ops/api/reshipments/%s/ship" % row["reship_uuid"]
        same = ops.post(path, json={"new_awb": "7X119057819", "new_courier": "DTDC"}, environ_overrides=IN)
        self.assertEqual(same.status_code, 400)
        # an AWB that cannot be the named courier's is refused before anything moves
        for courier, awb in (("DTDC", "7X3"), ("Delhivery", "7X200000001"), ("DTDC", "7X2000-0001")):
            bad = ops.post(path, json={"new_awb": awb, "new_courier": courier}, environ_overrides=IN)
            self.assertEqual((bad.status_code, bad.get_json()["error"]), (400, "awb_format"), (courier, awb))
        self.assertEqual(len(Stubs.shipped), 0)
        self.assertEqual(len(self._awbs(oid)), 1)
        # the courier platform booked the AWB and wrote its own row first: no duplicate
        self.cur.execute("INSERT INTO ops_shipping_awb (ow_order_id, tracking_number, courier, "
                         "awb_status, created_by) VALUES (%s,'7X200000001','DTDC','created','platform')", (oid,))
        self.db.commit()
        r = ops.post(path, json={"new_awb": "7X200000001", "new_courier": "DTDC"}, environ_overrides=IN)
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()["reship"]
        self.assertEqual((body["status"], body["new_awb"], body["original_awb"], body["shipped_by"]),
                         ("RESHIPPED", "7X200000001", "7X119057819", "ops-api-token"))
        self.assertEqual(self._awbs(oid), [("7X119057819", "DTDC"), ("7X200000001", "DTDC")])
        self.cur.execute("SELECT order_status_name FROM order_status WHERE order_id=%s "
                         "ORDER BY order_status_id", (oid,))
        self.assertEqual([s["order_status_name"] for s in self.cur.fetchall()],
                         ["Processed", "Shipped", "Returned", "Shipped"])
        self.assertEqual(len(Stubs.shipped), 1)
        # idempotent retry with the same AWB, refused with another
        self.assertEqual(ops.post(path, json={"new_awb": "7X200000001", "new_courier": "DTDC"},
                                  environ_overrides=IN).status_code, 200)
        self.assertEqual(ops.post(path, json={"new_awb": "7X3", "new_courier": "DTDC"},
                                  environ_overrides=IN).status_code, 409)
        self.assertEqual(len(Stubs.shipped), 1)
        self.assertEqual(len(self._awbs(oid)), 2)
        view = self._client(cid).get("/api/orders/%s/reship" % oid, environ_overrides=IN).get_json()["reship"]
        self.assertEqual((view["state"], view["new_awb"], view["new_courier"]),
                         ("RESHIPPED", "7X200000001", "DTDC"))

    def test_ops_routes_need_ops_auth(self):
        cid, oid, row = self._returned()
        for c in (self._client(), self._client(cid)):
            self.assertEqual(c.post("/ops/api/shipments/%s/return-received" % oid,
                                    environ_overrides=IN).status_code, 401)
            self.assertEqual(c.post("/ops/api/reshipments/%s/ship" % row["reship_uuid"],
                                    environ_overrides=IN).status_code, 401)
            self.assertEqual(c.get("/ops/reship", environ_overrides=IN).status_code, 401)
        bad = self._client()
        bad.environ_base["HTTP_AUTHORIZATION"] = "Bearer wrong"
        self.assertEqual(bad.get("/ops/api/reship/queue", environ_overrides=IN).status_code, 401)
        admin = self._client(email="lensebazaar@gmail.com")
        self.assertEqual(admin.get("/ops/api/reship/queue", environ_overrides=IN).status_code, 200)

    def test_ops_return_received_route_records_operator_and_notifies_once(self):
        cid = self._customer()
        oid = self._order(cid)
        ops = self._client(ops=True)
        path = "/ops/api/shipments/%s/return-received" % oid
        r = ops.post(path, json={"return_reason": "address not found"}, environ_overrides=IN)
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertTrue(body["created"])
        self.assertEqual(body["reship"]["ops_return_confirmed_by"], "ops-api-token")
        self.assertEqual(body["reship"]["return_reason"], "address not found")
        self.assertEqual(body["reship"]["original_awb"], "7X119057819")
        self.assertEqual(len(Stubs.mails), 1)
        self.assertFalse(ops.post(path, environ_overrides=IN).get_json()["created"])
        self.assertEqual(len(Stubs.mails), 1)
        self.assertEqual(len(self._events(oid, reship.EV_AVAILABLE)), 1)

    def test_token_caller_names_its_operator_in_the_audit(self):
        cid = self._customer()
        oid = self._order(cid)
        ops = self._client(ops=True)
        r = ops.post("/ops/api/shipments/%s/return-received" % oid,
                     json={"operator": " ravi@ops "}, environ_overrides=IN)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["reship"]["ops_return_confirmed_by"], "ops-api-token:ravi@ops")

    def test_only_the_returned_shipment_of_a_multi_line_order_is_affected(self):
        cid = self._customer()
        oid = self._order(cid, lines=3)
        other = self._order(cid, status="Delivered", awb="OTHER1")
        row = reship.confirm_returned(self.db, oid, "ops@optiwar.com")
        self.assertEqual(row["order_id"], oid)
        self.assertIsNone(reship.active_for_order(self.db, other))
        self.cur.execute("SELECT COUNT(*) AS n FROM order_reshipments WHERE order_id=%s", (oid,))
        self.assertEqual(self.cur.fetchone()["n"], 1)
        self.cur.execute("SELECT COUNT(*) AS n FROM orders WHERE order_id=%s", (oid,))
        self.assertEqual(self.cur.fetchone()["n"], 3)
        self.assertEqual(self._awbs(other), [("OTHER1", "DTDC")])

    # --------------------------------------------------------- notifications

    def test_return_started_is_told_once(self):
        cid = self._customer()
        oid = self._order(cid)
        first = reship.sweep_return_started(self.db, mailer=lambda *a: Stubs.mails.append(a))
        second = reship.sweep_return_started(self.db, mailer=lambda *a: Stubs.mails.append(a))
        self.assertGreaterEqual(first["notified"], 1)
        self.assertEqual([m for m in Stubs.mails if m[0] == "c@example.in"].__len__(), 1)
        self.assertEqual(second["notified"], 0)
        self.assertEqual(len(self._events(oid, reship.EV_RETURN_STARTED)), 1)
        # no reship row was created: RETURNING_TO_OPS is not RETURNED_TO_OPS
        self.assertIsNone(reship.active_for_order(self.db, oid))

    def test_availability_and_paid_notices_are_sent_once_each(self):
        cid, oid, row = self._returned()
        for _ in range(2):
            reship.notify(self.db, reship.EV_AVAILABLE, oid, cid, "optiwar.in",
                          reship_uuid=row["reship_uuid"])
        self.assertEqual(len(Stubs.mails), 1)
        self.assertIn(oid, Stubs.mails[0][1])
        c, rzp_order = self._start(cid, row)
        pay = _payment("pay_n1", rzp_order)
        Stubs.provider_payments[pay["id"]] = pay
        self.assertEqual(self._verify(c, row, pay).status_code, 200)
        self.assertEqual(self._verify(c, row, pay).status_code, 200)
        with _app().test_request_context(base_url="https://optiwar.in/"):
            reship_api.settle_and_notify(self.db, row["reship_uuid"], pay, "razorpay-webhook",
                                         "optiwar.in")
        self.assertEqual(len(Stubs.mails), 2)
        self.assertEqual(len([e for e in self._events(oid, reship.EV_NOTIFIED)]), 2)

    def test_a_failed_notification_changes_no_business_state(self):
        cid, oid, row = self._returned()

        def broken(*a):
            raise RuntimeError("smtp down")
        out = reship.notify(self.db, reship.EV_AVAILABLE, oid, cid, "optiwar.in",
                            reship_uuid=row["reship_uuid"], mailer=broken)
        self.assertEqual(out["email"], "FAILED")
        self.assertEqual(reship.by_uuid(self.db, row["reship_uuid"])["status"], "RETURNED")
        self.assertEqual(len(self._events(oid, reship.EV_NOTIFY_FAILED)), 1)

    def test_whatsapp_is_draft_only_until_approved(self):
        self.assertEqual(set(reship.WA_TEMPLATES), {"return_started", "reship_available",
                                                     "reship_paid"})
        cid, oid, row = self._returned()
        sent = []
        reship.notify(self.db, reship.EV_AVAILABLE, oid, cid, "optiwar.in",
                      reship_uuid=row["reship_uuid"], whatsapp=lambda *a: sent.append(a))
        self.assertEqual(sent, [])

    def test_every_reship_email_copies_admin_and_available_asks_to_check_address(self):
        cid, oid, row = self._returned()
        got = []
        reship.notify(self.db, reship.EV_AVAILABLE, oid, cid, "optiwar.in",
                      reship_uuid=row["reship_uuid"],
                      mailer=lambda to, subj, text: got.append(text))
        self.assertIn("delivery address and phone number", got[0])
        self.assertIn("support@optiwar.com", got[0])

        captured = {}

        class _Mail:
            def send(self, msg):
                captured["msg"] = msg

        class _Message:
            def __init__(self, **kw):
                self.__dict__.update(kw)
        import sys
        import types
        from unittest import mock
        from flask import Flask
        fm = types.ModuleType("flask_mail")
        fm.Message = _Message
        app = Flask("t")
        app.extensions["mail"] = _Mail()
        with mock.patch.dict(sys.modules, {"flask_mail": fm}), app.app_context():
            self._mail("shreya@example.com", "s", "t")
            self.assertEqual(captured["msg"].recipients, ["shreya@example.com"])
            self.assertEqual(captured["msg"].cc, ["admin@optiwar.com"])
            self._mail("admin@optiwar.com", "s", "t")
            self.assertEqual(captured["msg"].cc, [])


if __name__ == "__main__":
    unittest.main()
