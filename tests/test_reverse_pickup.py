"""Delhivery reverse pickup booked by Ops: one row per waybill, a replay
returns it without a second message, the forward AWB is never rewritten, the
customer is told once per channel and My Orders shows the scheduled pickup."""
import hashlib
import hmac
import json
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
                               ("reverse_pickup_cases", "order_id"),
                               ("reverse_pickup_ops_outbox", "order_id"),
                               ("ops_shipping_awb", "ow_order_id")):
                cur.execute("DELETE FROM %s WHERE %s=%%s" % (table, col), (oid,))
        for cid in self._customers:
            cur.execute("DELETE FROM customers WHERE customer_id=%s", (cid,))
        self.db.commit()

    def _order(self, site="in.optiwar.com", forward=FORWARD, fee="WAIVED"):
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
        if fee:
            self._case(oid, fee)
        self.db.commit()
        return cid, oid

    def _case(self, oid, fee_state):
        """A return case seeded directly, as the customer request / payment
        flow or an earlier waiver would have left it."""
        self.cur.execute(
            "INSERT INTO reverse_pickup_cases (case_uuid, order_id, case_no, source, fee_state, "
            "fee_amount_minor, fee_currency, created_by) VALUES (UUID(),%s,1,'test',%s,25000,'INR','test')",
            (oid, fee_state))
        self.db.commit()

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
        self.assertIn("/terms_and_conditions", text)
        self.assertNotIn("/terms-and-conditions", text)
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

    # ------------------------------------------------------- fee authority

    def _get(self, path, token=TOKEN):
        headers = {"Authorization": "Bearer %s" % token} if token else {}
        return self.client.get(path, headers=headers, environ_base={"HTTP_HOST": "optiwar.in"})

    def _outbox(self, oid):
        self.cur.execute("SELECT event, body, status FROM reverse_pickup_ops_outbox WHERE order_id=%s "
                         "ORDER BY id", (oid,))
        rows = self.cur.fetchall()
        self.db.commit()
        return rows

    def _legacy_pickup(self, oid, awb=AWB, reason="Other"):
        """A pickup booked before the fee flow: no case row."""
        self.cur.execute("INSERT INTO order_reverse_pickups (pickup_uuid, order_id, courier, awb, reason, "
                         "forward_awb, status, booked_by) VALUES (UUID(),%s,'Delhivery',%s,%s,%s,'BOOKED','ops')",
                         (oid, awb, reason, FORWARD))
        self.db.commit()

    def test_booking_refused_until_fee_paid_or_waived(self):
        for fee in (None, "DUE", "REFUNDED"):
            _c, oid = self._order(fee=fee)
            r = self._post(oid, self._body())
            self.assertEqual((r.status_code, r.get_json()["error"]), (409, "fee_not_settled"), fee)
            self.cur.execute("SELECT COUNT(*) AS n FROM order_reverse_pickups WHERE order_id=%s", (oid,))
            self.assertEqual(self.cur.fetchone()["n"], 0)
            self.assertEqual(list(self._outbox(oid)), [])
        self.assertEqual(self.mails, [])
        _c, paid = self._order(fee="PAID")
        r = self._post(paid, self._body(awb="36120510077777"))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["fee"]["state"], "PAID")

    def test_waiver_without_a_case_is_audited_once_and_asks_the_customer_nothing(self):
        _c, oid = self._order(fee=None)
        body = {"operator": "ops-user", "reason_code": "OWNER_DECISION"}
        r = self._post(oid, body, path="/fee/waive")
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertTrue(j["changed"])
        self.assertEqual(j["fee"]["state"], "WAIVED")
        self.assertEqual(j["fee"]["amount_minor"], 25000)
        self.assertEqual(j["fee"]["waiver"]["reason_code"], "OWNER_DECISION")
        self.assertEqual(j["fee"]["waiver"]["waived_by"], "ops-api-token:ops-user")
        again = self._post(oid, body, path="/fee/waive")
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.get_json()["changed"])
        self.assertEqual(again.get_json()["case_id"], j["case_id"])
        self.assertEqual((self.mails, self.wa), ([], []))
        self.assertEqual(sum("waived" in h for h in self._history(oid)), 1)
        self.cur.execute("SELECT COUNT(*) AS n FROM reship_events WHERE order_id=%s AND event_type=%s",
                         (oid, rp.EV_FEE_WAIVED))
        self.assertEqual(self.cur.fetchone()["n"], 1)
        out = self._outbox(oid)
        self.assertEqual([o["event"] for o in out], ["reverse_pickup.fee_waived"])
        payload = json.loads(out[0]["body"])
        self.assertEqual(payload["fee"], {"state": "WAIVED", "amount_minor": 25000,
                                          "refunded_minor": 0, "currency": "INR"})
        self.assertEqual(payload["data"]["reason_code"], "OWNER_DECISION")
        self.assertEqual(self._post(oid, self._body()).status_code, 200)

    def test_waiver_refusals(self):
        _c, oid = self._order(fee=None)
        cases = [({"reason_code": "BECAUSE"}, 422, "invalid_reason_code"),
                 ({"reason_code": "OTHER"}, 422, "note_required")]
        for body, status, code in cases:
            r = self._post(oid, body, path="/fee/waive")
            self.assertEqual((r.status_code, r.get_json()["error"]), (status, code))
        self.assertEqual(self._post(oid, {"reason_code": "OTHER", "note": "courier damage"},
                                    path="/fee/waive").status_code, 200)
        r = self._post(oid, {"reason_code": "GOODWILL"}, path="/fee/waive")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "waiver_exists"))
        _c, paid = self._order(fee="PAID")
        r = self._post(paid, {"reason_code": "GOODWILL"}, path="/fee/waive")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "fee_not_waivable"))
        _c, com = self._order(site="optiwar.com", fee=None)
        self.assertEqual(self._post(com, {"reason_code": "GOODWILL"}, path="/fee/waive").get_json()["error"],
                         "not_india_order")
        self.assertEqual(self._post("NOPE-1", {"reason_code": "GOODWILL"}, path="/fee/waive").status_code, 404)
        self.assertEqual(self._post(oid, {"reason_code": "GOODWILL"}, path="/fee/waive",
                                    token=None).status_code, 401)

    def test_pre_fee_booking_is_waived_and_its_reason_corrected_without_touching_the_awb(self):
        _c, oid = self._order(fee=None)
        self._legacy_pickup(oid)
        q = self._get("/ops/api/reverse-pickup/queue").get_json()
        mine = [i for i in q["items"] if i["order_ref"] == oid]
        self.assertEqual([(i["queue_state"], i["fee"]["state"]) for i in mine],
                         [("PICKUP_BOOKED", None)])
        r = self._post(oid, {"operator": "ops-user", "reason_code": "BOOKED_BEFORE_FEE_FLOW"},
                       path="/fee/waive")
        self.assertEqual(r.get_json()["fee"]["state"], "WAIVED")
        r = self._post(oid, {"operator": "ops-user", "awb": AWB, "reason": "MANUFACTURING DEFECT"},
                       path="/reason")
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertEqual((j["changed"], j["old_reason"], j["reason"], j["awb"]),
                         (True, "Other", "MANUFACTURING DEFECT", AWB))
        again = self._post(oid, {"awb": AWB, "reason": "MANUFACTURING DEFECT"}, path="/reason")
        self.assertFalse(again.get_json()["changed"])
        self.cur.execute("SELECT payload FROM reship_events WHERE order_id=%s AND event_type=%s",
                         (oid, rp.EV_REASON_CORRECTED))
        audits = [json.loads(a["payload"]) for a in self.cur.fetchall()]
        self.assertEqual(len(audits), 1)
        self.assertEqual({k: audits[0][k] for k in ("awb", "operator", "old_reason", "new_reason")},
                         {"awb": AWB, "operator": "ops-api-token:ops-user", "old_reason": "Other",
                          "new_reason": "MANUFACTURING DEFECT"})
        self.assertTrue(audits[0]["at"])
        state = self._get("/ops/api/shipments/OW-%s/reverse-pickup" % oid).get_json()
        self.assertEqual(state["fee"]["state"], "WAIVED")
        self.assertEqual(state["fee"]["waiver"]["reason_code"], "BOOKED_BEFORE_FEE_FLOW")
        self.assertEqual(state["reverse_pickup"]["awb"], AWB)
        self.assertEqual(state["reverse_pickup"]["status"], "BOOKED")
        self.assertEqual(state["request"]["reason"], "MANUFACTURING DEFECT")
        self.assertEqual(state["forward_shipment"]["original"], {"awb": FORWARD, "courier": "DTDC"})
        self.assertFalse(state["booking_allowed"])
        self.assertEqual((self.mails, self.wa), ([], []))
        self.assertEqual(self._post(oid, {"awb": "36120510055555", "reason": "x"},
                                    path="/reason").status_code, 404)
        self.assertEqual(self._post(oid, {"awb": AWB}, path="/reason").get_json()["error"],
                         "reason_required")

    def test_state_and_queue(self):
        _c, due = self._order(fee="DUE")
        _c, waived = self._order(fee="WAIVED")
        _c, none = self._order(fee=None)
        self.assertEqual(self._get("/ops/api/shipments/%s/reverse-pickup" % due, token=None).status_code, 401)
        self.assertEqual(self._get("/ops/api/reverse-pickup/queue", token="wrong").status_code, 401)
        self.assertEqual(self._get("/ops/api/shipments/NOPE-1/reverse-pickup").status_code, 404)
        s = self._get("/ops/api/shipments/%s/reverse-pickup" % due).get_json()
        for key in ("request", "fee", "reverse_pickup", "received", "inspection", "consent",
                    "forward_shipment"):
            self.assertIn(key, s)
        self.assertEqual(set(s["fee"]), {"state", "amount_minor", "refunded_minor", "currency",
                                         "paid_at", "waiver", "refunds"})
        self.assertEqual((s["fee"]["state"], s["queue_state"], s["label"], s["booking_allowed"]),
                         ("DUE", "AWAITING_FEE", "AWAITING ₹250", False))
        n = self._get("/ops/api/shipments/%s/reverse-pickup" % none).get_json()
        self.assertEqual((n["fee"]["state"], n["case_id"], n["reverse_pickup"]), (None, None, None))
        r = self._get("/ops/api/reverse-pickup/queue")
        text = r.get_data(as_text=True)
        items = {i["order_ref"]: i for i in r.get_json()["items"]}
        self.assertEqual(items[due]["label"], "AWAITING ₹250")
        self.assertEqual(items[waived]["label"], "READY TO BOOK PICKUP")
        self.assertNotIn(none, items)
        for leak in ("rp@example.in", "919999900000", "Test Customer"):
            self.assertNotIn(leak, text)

    def test_ops_events_are_signed_over_the_exact_body_and_retried_until_2xx(self):
        _c, oid = self._order()
        self.assertEqual(self._post(oid, self._body()).status_code, 200)
        self.assertEqual([o["event"] for o in self._outbox(oid)],
                         ["reverse_pickup.booked", "reverse_pickup.notified"])
        self.assertEqual({o["status"] for o in self._outbox(oid)}, {"PENDING"})
        env = {"RESHIP_OPS_WEBHOOK_URL": "https://ops.example/hook"}
        self.assertEqual(rp.deliver_ops_events(self.db, environ=env, http_post=lambda *a: 200)["failed"], 1)
        env["RESHIP_OPS_WEBHOOK_SECRET"] = "s3cret"
        posts, answers = [], [500, 200, 200]

        def post(url, body, headers):
            posts.append((body, headers))
            return answers.pop(0)
        self.cur.execute("UPDATE reverse_pickup_ops_outbox SET status='SENT' WHERE order_id<>%s "
                         "AND status<>'SENT'", (oid,))
        self.db.commit()
        first = rp.deliver_ops_events(self.db, environ=env, http_post=post)
        self.assertEqual((first["sent"], first["failed"]), (0, 1))
        self.assertEqual(len(posts), 1)
        second = rp.deliver_ops_events(self.db, environ=env, http_post=post)
        self.assertEqual(second["sent"], 2)
        self.assertEqual({o["status"] for o in self._outbox(oid)}, {"SENT"})
        body, headers = posts[1]
        self.assertEqual(posts[0][0], body)
        self.assertEqual(headers["X-Optiwar-Signature"],
                         "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest())
        payload = json.loads(body)
        self.assertEqual(headers["X-Optiwar-Event"], "reverse_pickup.booked")
        self.assertEqual(headers["X-Optiwar-Event-Id"], payload["event_id"])
        self.assertEqual(payload["fee"]["state"], "WAIVED")
        self.assertEqual(payload["awb"], AWB)
        self.assertNotIn("rp@example.in", body.decode())
        self.assertEqual(json.loads(posts[2][0])["data"], {"notified_event": "reverse_pickup.booked",
                                                           "channel": "email"})
        self.assertEqual(rp.deliver_ops_events(self.db, environ=env, http_post=post)["sent"], 0)


if __name__ == "__main__":
    unittest.main()
