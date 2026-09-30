"""Delhivery reverse pickup booked by Ops: one row per waybill, a replay
returns it without a second message, the forward AWB is never rewritten, the
customer is told once per channel and My Orders shows the scheduled pickup."""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

import test_reship as base  # noqa: E402  (the name the reship harness binds its stubs under)
from tests.test_paid_order_pipeline import DDL, _connect  # noqa: E402
from tests.test_razorpay_settlement import EXTRA_DDL  # noqa: E402

PKG, TOKEN, AWB_DDL, _app = base.PKG, base.TOKEN, base.AWB_DDL, base._app

reship = sys.modules[PKG + ".reship"]
import importlib  # noqa: E402
rp = importlib.import_module(PKG + ".reverse_pickup")
customer_orders = sys.modules[PKG + ".customer_orders"]
reship_api = sys.modules[PKG + ".reship_api"]

AWB = "36120510012345"
FORWARD = "7X118669628"


class ReversePickupTest(unittest.TestCase):

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
        rp._SCHEMA_READY = False
        rp.ensure_schema(cls.db)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "db", None) is not None:
            cls.db.close()

    def setUp(self):
        self._get_db = reship_api.get_db
        reship_api.get_db = lambda: self.db
        os.environ[rp.ENABLED_ENV] = "true"
        os.environ[rp.CUSTOMER_ENV] = "true"
        os.environ.pop(rp.WA_APPROVED_ENV, None)
        self.mails, self.wa = [], []
        self._mail, self._wa = reship._default_mailer, reship._default_whatsapp
        reship._default_mailer = lambda to, subj, text: self.mails.append((to, subj, text))
        reship._default_whatsapp = lambda phone, tpl, comps: (self.wa.append((phone, tpl, comps))
                                                               or {"ok": True})
        self.cur = self.db.cursor()
        self._orders, self._customers = [], []
        self.client = _app().test_client()

    def tearDown(self):
        reship._default_mailer, reship._default_whatsapp = self._mail, self._wa
        reship_api.get_db = self._get_db
        os.environ.pop(rp.ENABLED_ENV, None)
        os.environ.pop(rp.CUSTOMER_ENV, None)
        os.environ.pop(rp.WA_APPROVED_ENV, None)
        self.db.rollback()
        cur = self.db.cursor()
        for oid in self._orders:
            for table, col in (("orders", "order_id"), ("order_history", "order_id"),
                               ("order_reverse_pickups", "order_id"), ("reship_events", "order_id"),
                               ("ops_shipping_awb", "ow_order_id")):
                cur.execute("DELETE FROM %s WHERE %s=%%s" % (table, col), (oid,))
        for cid in self._customers:
            cur.execute("DELETE FROM customers WHERE customer_id=%s", (cid,))
        self.db.commit()

    def _order(self, site="in.optiwar.com", forward=FORWARD):
        self.cur.execute("INSERT INTO customers (customer_name, customer_email, customer_phone) "
                         "VALUES ('Test Customer', 'rp@example.in', '919999900000')")
        cid = self.cur.lastrowid
        self._customers.append(cid)
        oid = "RP%s" % os.urandom(5).hex().upper()
        self._orders.append(oid)
        self.cur.execute("INSERT INTO orders (order_id, customer_id, product_id, order_total, "
                         "site_from) VALUES (%s,%s,1,949,%s)", (oid, cid, site))
        if forward:
            self.cur.execute("INSERT INTO ops_shipping_awb (ow_order_id, tracking_number, courier, "
                             "awb_status, created_by) VALUES (%s,%s,'DTDC','created','ops')",
                             (oid, forward))
        self.db.commit()
        return cid, oid

    def _post(self, oid, body, path="", token=TOKEN):
        headers = {"Authorization": "Bearer %s" % token} if token else {}
        return self.client.post("/ops/api/shipments/%s/reverse-pickup%s" % (oid, path),
                                json=body, headers=headers, environ_base={"HTTP_HOST": "optiwar.in"})

    def _body(self, awb=AWB, **kw):
        body = {"operator": "ops-user@lensbazaar", "courier": "Delhivery", "awb": awb,
                "reference": "RP1", "reason": "Wrong power / Rx issue", "forward_awb": FORWARD,
                "track_url": "https://evil.example/phish",
                "pickup": {"name": "N S", "address": "x", "city": "Basti", "pin": "272001",
                           "phone": "9000000000"}}
        body.update(kw)
        return body

    def _history(self, oid):
        self.cur.execute("SELECT order_history_content AS c FROM order_history WHERE order_id=%s",
                         (oid,))
        return [r["c"] for r in self.cur.fetchall()]

    # ------------------------------------------------------------------ tests

    def test_booking_records_notifies_once_and_replay_is_silent(self):
        cid, oid = self._order()
        r = self._post("OW-" + oid, self._body())
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertTrue(j["created"])
        view = j["reverse_pickup"]
        self.assertEqual(view["status"], "BOOKED")
        self.assertEqual(view["forward_awb"], FORWARD)
        self.assertEqual([s["channel"] for s in view["notification_state"]["sent"]], ["email"])
        self.assertEqual(view["notification_state"]["failed"], 0)
        self.assertIn({"channel": "whatsapp", "reason": "template_not_approved"},
                      view["notification_state"]["skipped"])
        self.assertEqual(len(self.mails), 1)
        text = self.mails[0][2]
        self.assertIn("https://www.delhivery.com/track-v2/package/%s" % AWB, text)
        self.assertNotIn("evil.example", text)
        self.assertIn("do not hand it to any other courier", text)
        self.assertIn("clause 9B", text)
        self.assertIn("reschedule, reply to this email or write to admin@optiwar.com", text)
        self.assertNotIn("Wrong power", text)
        self.assertEqual(self.wa, [])
        self.assertTrue(any("Reverse pickup booked with Delhivery, AWB %s" % AWB in h
                            for h in self._history(oid)))

        again = self._post(oid, self._body())
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.get_json()["created"])
        self.assertEqual(again.get_json()["reverse_pickup"]["id"], view["id"])
        self.assertEqual(len(self.mails), 1)
        self.assertEqual(len(self._history(oid)), 1)
        self.cur.execute("SELECT COUNT(*) AS n FROM order_reverse_pickups WHERE order_id=%s", (oid,))
        self.assertEqual(self.cur.fetchone()["n"], 1)

    def test_whatsapp_goes_out_only_when_templates_approved(self):
        os.environ[rp.WA_APPROVED_ENV] = "1"
        _cid, oid = self._order()
        j = self._post(oid, self._body()).get_json()
        self.assertEqual(sorted(s["channel"] for s in j["reverse_pickup"]["notification_state"]["sent"]),
                         ["email", "whatsapp"])
        self.assertEqual(len(self.wa), 1)
        phone, tpl, comps = self.wa[0]
        self.assertEqual(tpl, "reverse_pickup_booked_v2")
        self.assertEqual(phone, "919999900000")
        self.assertEqual(comps["body_2"]["value"], AWB)

    def test_a_failed_whatsapp_is_counted_not_fatal(self):
        os.environ[rp.WA_APPROVED_ENV] = "1"
        reship._default_whatsapp = lambda *a: {"ok": False, "error": "template rejected"}
        _cid, oid = self._order()
        r = self._post(oid, self._body())
        self.assertEqual(r.status_code, 200)
        state = r.get_json()["reverse_pickup"]["notification_state"]
        self.assertEqual(state["failed"], 1)
        self.assertEqual([s["channel"] for s in state["sent"]], ["email"])

    def test_refusals(self):
        _cid, oid = self._order()
        cases = [
            (self._body(courier="DTDC"), 422, "courier_not_supported"),
            (self._body(awb="ABC123"), 422, "invalid_awb"),
            (self._body(forward_awb="7X000000000"), 409, "forward_awb_mismatch"),
        ]
        for body, status, code in cases:
            r = self._post(oid, body)
            self.assertEqual((r.status_code, r.get_json()["error"]), (status, code), body)
            self.assertFalse(r.get_json()["ok"])
        self.assertEqual(self._post("NOPE-1", self._body()).status_code, 404)
        self.assertEqual(self._post(oid, self._body(), token="wrong").status_code, 401)
        self.assertEqual(self._post(oid, self._body(), token=None).status_code, 401)
        _c, com = self._order(site="optiwar.com")
        self.assertEqual(self._post(com, self._body()).get_json()["error"], "not_india_order")
        self.assertEqual(self.mails, [])

    def test_one_active_pickup_per_order_and_awb_per_order(self):
        _cid, oid = self._order()
        self.assertEqual(self._post(oid, self._body()).status_code, 200)
        r = self._post(oid, self._body(awb="36120510099999"))
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "active_pickup_exists"))
        _c2, other = self._order()
        r = self._post(other, self._body())
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "awb_in_use"))

    def test_cancel_is_idempotent_and_frees_the_order(self):
        _cid, oid = self._order()
        self._post(oid, self._body())
        r = self._post(oid, {"operator": "ops-user", "awb": AWB}, path="/cancel")
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertTrue(j["changed"])
        self.assertEqual(j["reverse_pickup"]["status"], "CANCELLED")
        self.assertEqual([s["channel"] for s in j["reverse_pickup"]["notification_state"]["sent"]],
                         ["email"])
        self.assertEqual(len(self.mails), 2)
        self.assertIn("cancelled", self.mails[1][1].lower())
        again = self._post(oid, {"operator": "ops-user", "awb": AWB}, path="/cancel")
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.get_json()["changed"])
        self.assertEqual(len(self.mails), 2)
        r = self._post(oid, self._body())
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "awb_cancelled"))
        self.assertEqual(self._post(oid, self._body(awb="36120510099999")).status_code, 200)
        self.assertEqual(self._post(oid, {"awb": "36120510055555"}, path="/cancel").status_code, 404)

    def test_disabled_flag_refuses_before_anything_is_stored(self):
        os.environ.pop(rp.ENABLED_ENV, None)
        _cid, oid = self._order()
        r = self._post(oid, self._body())
        self.assertEqual((r.status_code, r.get_json()["error"]), (503, "disabled"))
        self.cur.execute("SELECT COUNT(*) AS n FROM order_reverse_pickups WHERE order_id=%s", (oid,))
        self.assertEqual(self.cur.fetchone()["n"], 0)

    def test_ops_only_mode_records_but_tells_the_customer_nothing(self):
        os.environ.pop(rp.CUSTOMER_ENV, None)
        os.environ[rp.WA_APPROVED_ENV] = "1"
        _cid, oid = self._order()
        r = self._post(oid, self._body())
        self.assertEqual(r.status_code, 200, r.get_json())
        state = r.get_json()["reverse_pickup"]["notification_state"]
        self.assertEqual(state["sent"], [])
        self.assertEqual(state["skipped"],
                         [{"channel": "email", "reason": "customer_notifications_off"},
                          {"channel": "whatsapp", "reason": "customer_notifications_off"}])
        self.assertTrue(any("Reverse pickup booked with Delhivery" in h for h in self._history(oid)))
        c = self._post(oid, {"awb": AWB}, path="/cancel")
        self.assertEqual(c.get_json()["reverse_pickup"]["status"], "CANCELLED")
        self.assertEqual((self.mails, self.wa), ([], []))
        self.cur.execute("SELECT COUNT(*) AS n FROM reship_events WHERE order_id=%s "
                         "AND event_type=%s", (oid, rp.EV_NOTIFIED))
        self.assertEqual(self.cur.fetchone()["n"], 0)

    def test_my_orders_card(self):
        cid, oid = self._order()
        self._post(oid, self._body())
        orders = [{"order_id": oid, "stage_label": "Delivered", "stage_tone": "shipped"}]
        customer_orders.attach_reverse_pickup(orders, rp.latest_for_customer(self.db, cid))
        card = orders[0]["reverse_pickup"]
        self.assertEqual(card["state"], "BOOKED")
        self.assertEqual(card["awb"], AWB)
        self.assertEqual(orders[0]["stage_label"], "Reverse pickup scheduled")
        self.assertNotIn("reason", card)
        self._post(oid, {"awb": AWB}, path="/cancel")
        orders = [{"order_id": oid, "stage_label": "Delivered", "stage_tone": "shipped"}]
        customer_orders.attach_reverse_pickup(orders, rp.latest_for_customer(self.db, cid))
        self.assertEqual(orders[0]["reverse_pickup"]["state"], "CANCELLED")
        self.assertEqual(orders[0]["stage_label"], "Delivered")


if __name__ == "__main__":
    unittest.main()
