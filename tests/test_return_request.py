"""A customer's return request (reverse pickup, phase 3a): only the signed-in
owner of a delivered India order asks, within 7 days of delivery, with the
versioned declarations and photos; Ops approves, asks for more or declines
before any fee or pickup, and the photos reach Ops only through a signed,
audited link."""
import io
import os
import shutil
import sys
import tempfile
import time
import unittest

from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

import test_reship as base  # noqa: E402
from tests.test_paid_order_pipeline import DDL, _connect  # noqa: E402
from tests.test_razorpay_settlement import EXTRA_DDL  # noqa: E402

PKG, TOKEN, AWB_DDL, _app = base.PKG, base.TOKEN, base.AWB_DDL, base._app

import importlib  # noqa: E402
reship = sys.modules[PKG + ".reship"]
rp = importlib.import_module(PKG + ".reverse_pickup")
rr = importlib.import_module(PKG + ".return_request")
ra = importlib.import_module(PKG + ".return_assistant")
customer_orders = sys.modules[PKG + ".customer_orders"]
reship_api = sys.modules[PKG + ".reship_api"]


def _png(seed=0, fmt="PNG"):
    img = Image.new("RGB", (48, 48), (seed * 40 % 255, 120, 200))
    for x in range(48):
        img.putpixel((x, (x + seed) % 48), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


class ReturnRequestTest(unittest.TestCase):

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
        rr._SCHEMA_READY = False
        rr.ensure_schema(cls.db)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "db", None) is not None:
            cls.db.close()

    def setUp(self):
        self._get_db = reship_api.get_db
        reship_api.get_db = lambda: self.db
        self.upload = tempfile.mkdtemp(prefix="rp-photos-")
        self._dir = reship_api._rp_upload_dir
        reship_api._rp_upload_dir = lambda: self.upload
        os.environ[rp.ENABLED_ENV] = "true"
        os.environ[rp.CUSTOMER_ENV] = "true"
        self.mails = []
        self._mail, self._wa = reship._default_mailer, reship._default_whatsapp
        reship._default_mailer = lambda to, subj, text: self.mails.append((to, subj, text))
        reship._default_whatsapp = lambda phone, tpl, comps: {"ok": True}
        self.cur = self.db.cursor()
        self._orders, self._customers = [], []
        self.app = _app()

    def tearDown(self):
        reship._default_mailer, reship._default_whatsapp = self._mail, self._wa
        reship_api.get_db = self._get_db
        reship_api._rp_upload_dir = self._dir
        shutil.rmtree(self.upload, ignore_errors=True)
        os.environ.pop(rp.ENABLED_ENV, None)
        os.environ.pop(rp.CUSTOMER_ENV, None)
        self.db.rollback()
        cur = self.db.cursor()
        for oid in self._orders:
            for table, col in (("orders", "order_id"), ("order_history", "order_id"),
                               ("order_status", "order_id"),
                               ("order_reverse_pickups", "order_id"), ("reship_events", "order_id"),
                               ("reverse_pickup_cases", "order_id"),
                               ("reverse_pickup_photos", "order_id"),
                               ("reverse_pickup_ops_outbox", "order_id"),
                               ("reverse_pickup_notifications", "order_id"),
                               ("ops_shipping_awb", "ow_order_id")):
                cur.execute("DELETE FROM %s WHERE %s=%%s" % (table, col), (oid,))
        for cid in self._customers:
            cur.execute("DELETE FROM customers WHERE customer_id=%s", (cid,))
        self.db.commit()

    # ------------------------------------------------------------ helpers

    def _order(self, site="in.optiwar.com", delivered_days=2, is_test=0, status="Complete"):
        self.cur.execute("INSERT INTO customers (customer_name, customer_email, customer_phone) "
                         "VALUES ('Test Customer', 'rr@example.in', '919999900000')")
        cid = self.cur.lastrowid
        self._customers.append(cid)
        oid = "RR%s" % os.urandom(5).hex().upper()
        self._orders.append(oid)
        self.cur.execute("INSERT INTO orders (order_id, customer_id, product_id, order_total, site_from, "
                         "is_test) VALUES (%s,%s,1,949,%s,%s)", (oid, cid, site, is_test))
        self.cur.execute("INSERT INTO order_status (order_status_name, order_id, created_at) "
                         "VALUES ('Shipped', %s, NOW() - INTERVAL 9 DAY)", (oid,))
        if status:
            if delivered_days is None:
                self.cur.execute("INSERT INTO order_status (order_status_name, order_id) VALUES (%s,%s)",
                                 (status, oid))
            else:
                self.cur.execute("INSERT INTO order_status (order_status_name, order_id, created_at) "
                                 "VALUES (%s,%s, NOW() - INTERVAL %s HOUR)",
                                 (status, oid, int(delivered_days * 24)))
        self.db.commit()
        return cid, oid

    def _client(self, customer_id=None):
        c = self.app.test_client()
        if customer_id is not None:
            with self.app.test_request_context():
                value = self.app.session_interface.get_signing_serializer(self.app).dumps(
                    {"user_id": customer_id})
            for host in ("optiwar.in", "optiwar.com"):
                c.set_cookie("session", value, domain=host)
        return c

    def _form(self, reason="MANUFACTURING_DEFECT", photos=2, **kw):
        data = {"reason": reason,
                "description": "The left hinge snapped the second day I wore them.",
                "declarations_version": rr.DECLARATIONS_VERSION,
                "declaration": [str(i) for i in range(1, len(rr.DECLARATIONS) + 1)],
                "photos": (photos if isinstance(photos, list)
                           else [(io.BytesIO(_png(i)), "p%d.png" % i) for i in range(photos)])}
        data.update(kw)
        return data

    def _submit(self, cid, oid, host="optiwar.in", client=None, **kw):
        c = client or self._client(cid)
        return c.post("/api/orders/%s/return" % oid, data=self._form(**kw),
                      content_type="multipart/form-data", headers={"Origin": "https://%s" % host},
                      environ_overrides={"HTTP_HOST": host})

    def _ops(self, oid, path="", body=None, method="post"):
        c = self.app.test_client()
        kw = {"headers": {"Authorization": "Bearer %s" % TOKEN},
              "environ_overrides": {"HTTP_HOST": "optiwar.in"}}
        url = "/ops/api/shipments/%s/reverse-pickup%s" % (oid, path)
        if method == "get":
            return c.get(url, **kw)
        return c.post(url, json=body or {}, **kw)

    def _decide(self, oid, decision, note=""):
        return self._ops(oid, "/request/decision",
                         {"decision": decision, "note": note, "operator": "ops@lensbazaar"})

    def _case(self, oid):
        return rp.case_for_order(self.db, oid)

    # ------------------------------------------------------------ the customer's request

    def test_request_opens_a_submitted_case_with_sealed_declarations_and_photos(self):
        cid, oid = self._order()
        r = self._submit(cid, oid)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.db.commit()
        case = self._case(oid)
        self.assertEqual(case["request_status"], rp.REQ_SUBMITTED)
        self.assertEqual(case["fee_state"], rp.FEE_DUE)
        self.assertEqual(case["source"], "customer")
        self.assertEqual(case["reason_code"], "MANUFACTURING_DEFECT")
        self.assertEqual(case["declarations_version"], rr.DECLARATIONS_VERSION)
        self.assertEqual(case["declarations_sha256"], rr.declarations_sha256())
        self.assertIsNotNone(case["declarations_accepted_at"])
        self.assertTrue(case["declarations_ip"])
        self.cur.execute("SELECT body, sha256 FROM policy_versions WHERE kind=%s AND version=%s",
                         (rr.POLICY_KIND, rr.DECLARATIONS_VERSION))
        sealed = self.cur.fetchone()
        self.assertEqual(sealed["sha256"], case["declarations_sha256"])
        self.assertIn(rr.DECLARATIONS[2], sealed["body"])
        photos = rr.photos_for(self.db, case["case_uuid"])
        self.assertEqual([p["position"] for p in photos], [1, 2])
        for p in photos:
            with open(os.path.join(self.upload, p["stored_name"]), "rb") as fh:
                self.assertTrue(fh.read(3) == b"\xff\xd8\xff")
        self.cur.execute("SELECT event FROM reverse_pickup_ops_outbox WHERE order_id=%s", (oid,))
        self.assertEqual([r["event"] for r in self.cur.fetchall()], [rp.EV_REQUESTED, rp.EV_NOTIFIED])
        self.assertEqual([m[1] for m in self.mails], ["Optiwar Return Request Received"])
        self.assertIn("Manufacturing defect", self.mails[0][2])

    def test_a_second_request_while_one_is_open_is_refused(self):
        cid, oid = self._order()
        self.assertEqual(self._submit(cid, oid).status_code, 200)
        r = self._submit(cid, oid)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"], "return_exists")
        self.assertEqual(len(self.mails), 1)

    def test_only_the_signed_in_owner_on_the_india_site(self):
        cid, oid = self._order()
        r = self._submit(None, oid, client=self._client())
        self.assertEqual(r.status_code, 401)
        other, _ = self._order()
        self.assertEqual(self._submit(other, oid).status_code, 404)
        self.assertEqual(self._submit(cid, oid, host="optiwar.com").status_code, 404)
        ccid, coid = self._order(site="optiwar.com")
        self.assertEqual(self._submit(ccid, coid).status_code, 404)
        os.environ[rp.CUSTOMER_ENV] = "false"
        self.assertEqual(self._submit(cid, oid).status_code, 404)
        self.assertIsNone(self._case(oid))

    def test_delivered_within_seven_days_and_not_a_test_order(self):
        for kw, code in (({"status": "Shipped"}, "not_delivered"),
                         ({"delivered_days": 8}, "return_window_closed"),
                         ({"delivered_days": None}, "delivery_date_unknown"),
                         ({"is_test": 1}, "test_order")):
            cid, oid = self._order(**kw)
            r = self._submit(cid, oid)
            self.assertEqual((r.status_code, r.get_json()["error"]), (409, code), kw)
            self.assertIsNone(self._case(oid))
        cid, oid = self._order(delivered_days=6.9)
        self.assertEqual(self._submit(cid, oid).status_code, 200)

    def test_form_is_checked_on_the_server(self):
        cid, oid = self._order()
        cases = (
            ({"photos": 1}, "photos_required"),
            ({"photos": 7}, "too_many_photos"),
            ({"reason": "BORED"}, "invalid_reason"),
            ({"description": "broken"}, "description_required"),
            ({"declaration": ["1", "2", "3"]}, "declarations_required"),
            ({"declarations_version": "rp-declarations-old"}, "declarations_outdated"),
            ({"photos": [(io.BytesIO(_png(1, "GIF")), "a.gif"), (io.BytesIO(_png(2)), "b.png")]},
             "invalid_photo"),
            ({"photos": [(io.BytesIO(b"<svg>" * 40), "a.png"), (io.BytesIO(_png(2)), "b.png")]},
             "invalid_photo"),
        )
        for kw, code in cases:
            r = self._submit(cid, oid, **kw)
            self.assertEqual(r.get_json()["error"], code, kw)
            self.assertGreaterEqual(r.status_code, 400)
        self.assertIsNone(self._case(oid))
        self.assertEqual(os.listdir(self.upload), [])
        self.assertEqual(self._submit(cid, oid, reason="WRONG_ITEM", photos=0).status_code, 200)

    def test_webp_and_jpeg_are_accepted(self):
        cid, oid = self._order()
        photos = [(io.BytesIO(_png(1, "WEBP")), "a.webp"), (io.BytesIO(_png(2, "JPEG")), "b.jpg")]
        self.assertEqual(self._submit(cid, oid, photos=photos).status_code, 200)

    # ------------------------------------------------------------ Ops' decision

    def test_no_pickup_before_approval_then_the_fee_gate(self):
        cid, oid = self._order()
        self._submit(cid, oid)
        body = base_body()
        r = self._ops(oid, "", body)
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "request_not_approved"))
        self.assertEqual(self._decide(oid, rp.REQ_APPROVED).status_code, 200)
        r = self._ops(oid, "", body)
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "fee_not_settled"))

    def test_approve_is_emailed_once_and_is_final(self):
        cid, oid = self._order()
        self._submit(cid, oid)
        r = self._decide(oid, rp.REQ_APPROVED)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["changed"])
        self.assertEqual(r.get_json()["request"]["status"], rp.REQ_APPROVED)
        r = self._decide(oid, rp.REQ_APPROVED)
        self.assertFalse(r.get_json()["changed"])
        r = self._decide(oid, rp.REQ_DECLINED, "Not a manufacturing fault.")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "decision_exists"))
        subjects = [m[1] for m in self.mails]
        self.assertEqual(subjects.count("Optiwar Return Request Approved"), 1)
        approved = [m[2] for m in self.mails if m[1] == "Optiwar Return Request Approved"][0]
        self.assertIn(rp.MY_ORDERS_URL_IN, approved)
        self.cur.execute("SELECT event FROM reverse_pickup_ops_outbox WHERE order_id=%s ORDER BY id",
                         (oid,))
        self.assertEqual([r["event"] for r in self.cur.fetchall()],
                         [rp.EV_REQUESTED, rp.EV_NOTIFIED, rp.EV_REQUEST_DECIDED, rp.EV_NOTIFIED])

    def test_approval_of_a_waived_fee_says_nothing_is_due(self):
        cid, oid = self._order()
        self._submit(cid, oid)
        self.assertEqual(self._ops(oid, "/fee/waive", {"reason_code": "GOODWILL",
                                                       "operator": "ops"}).status_code, 200)
        self._decide(oid, rp.REQ_APPROVED)
        text = [m[2] for m in self.mails if m[1] == "Optiwar Return Request Approved"][0]
        self.assertIn("has been waived", text)
        self.assertNotIn(rp.MY_ORDERS_URL_IN, text)

    def test_more_information_then_approve(self):
        cid, oid = self._order()
        self._submit(cid, oid)
        r = self._decide(oid, rp.REQ_INFO, "")
        self.assertEqual(r.get_json()["error"], "note_required")
        r = self._decide(oid, rp.REQ_INFO, "Please send a photo of the hinge from the side.")
        self.assertEqual(r.status_code, 200)
        info = [m[2] for m in self.mails if "Information Needed" in m[1]]
        self.assertEqual(len(info), 1)
        self.assertIn("photo of the hinge", info[0])
        self.assertEqual(self._decide(oid, rp.REQ_INFO, "Another note here.").status_code, 409)
        self.assertEqual(self._decide(oid, rp.REQ_APPROVED).status_code, 200)

    def test_decline_closes_the_case_and_a_new_request_is_allowed(self):
        cid, oid = self._order()
        self._submit(cid, oid)
        r = self._decide(oid, rp.REQ_DECLINED, "The damage shown is from an impact.")
        self.assertEqual(r.status_code, 200)
        self.db.commit()
        case = self._case(oid)
        self.assertEqual(case["completed_outcome"], rp.OUTCOME_NOT_APPROVED)
        self.assertIn("from an impact", [m[2] for m in self.mails if m[1] == "Optiwar Return Request Update"][0])
        r = self._submit(cid, oid)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.db.commit()
        self.assertEqual(self._case(oid)["case_no"], 2)

    def test_decision_needs_a_customer_request(self):
        cid, oid = self._order()
        self.assertEqual(self._decide(oid, rp.REQ_APPROVED).get_json()["error"], "no_case")
        self._ops(oid, "/fee/waive", {"reason_code": "GOODWILL", "operator": "ops"})
        self.assertEqual(self._decide(oid, rp.REQ_APPROVED).get_json()["error"], "no_request")
        self.assertEqual(self._decide(oid, "MAYBE").get_json()["error"], "invalid_decision")

    def test_decision_needs_the_ops_token(self):
        cid, oid = self._order()
        self._submit(cid, oid)
        r = self.app.test_client().post("/ops/api/shipments/%s/reverse-pickup/request/decision" % oid,
                                        json={"decision": rp.REQ_APPROVED},
                                        environ_overrides={"HTTP_HOST": "optiwar.in"})
        self.assertIn(r.status_code, (401, 403))
        self.db.commit()
        self.assertEqual(self._case(oid)["request_status"], rp.REQ_SUBMITTED)

    # ------------------------------------------------------------ photos for Ops

    def test_ops_reads_photos_only_through_a_signed_audited_link(self):
        cid, oid = self._order()
        self._submit(cid, oid)
        state = self._ops(oid, method="get").get_json()
        photos = state["request"]["photos"]
        self.assertEqual(len(photos), 2)
        c = self.app.test_client()
        r = c.get(photos[0]["url"], environ_overrides={"HTTP_HOST": "optiwar.in"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/jpeg")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        r.close()
        self.db.commit()
        self.cur.execute("SELECT COUNT(*) AS n FROM reship_events WHERE order_id=%s AND event_type=%s",
                         (oid, rr.EV_PHOTO_VIEWED))
        self.assertEqual(self.cur.fetchone()["n"], 1)
        bad = photos[0]["url"].replace("sig=", "sig=0")
        self.assertEqual(c.get(bad).status_code, 404)
        case = self._case(oid)
        expired = rr.photo_link("test-only", case["case_uuid"], 1, now=time.time() - 3600)
        self.assertEqual(c.get(expired).status_code, 404)
        unsigned = "/ops/api/reverse-pickup/photos/%s/1" % case["case_uuid"]
        self.assertEqual(c.get(unsigned).status_code, 404)

    # ------------------------------------------------------------ My Orders and the assistant

    def test_my_orders_cards_follow_the_request_and_never_carry_ops_fields(self):
        cid, oid = self._order()
        cards = rr.customer_cards(self.db, cid, [oid])
        self.assertEqual(cards[oid]["state"], rr.CARD_CAN_REQUEST)
        self.assertEqual(cards[oid]["form"]["declarations_version"], rr.DECLARATIONS_VERSION)
        other, _ = self._order()
        self.assertEqual(rr.customer_cards(self.db, other, [oid]), {})
        self._submit(cid, oid)
        card = rr.customer_cards(self.db, cid, [oid])[oid]
        self.assertEqual((card["state"], card["can_request"], card["form"]),
                         (rr.CARD_SUBMITTED, False, None))
        self._decide(oid, rp.REQ_APPROVED, "internal: checked by Ravi")
        card = rr.customer_cards(self.db, cid, [oid])[oid]
        self.assertEqual(card["state"], rr.CARD_APPROVED_FEE_DUE)
        for key in ("ip", "declarations_ip", "decided_by", "operator", "note"):
            self.assertNotIn(key, card)
        orders = [{"order_id": oid}]
        customer_orders.attach_reverse_pickup(orders, {}, {oid: card})
        self.assertEqual(orders[0]["stage_label"], "Return requested")
        self.assertEqual(orders[0]["return_request"]["state"], rr.CARD_APPROVED_FEE_DUE)

    def test_window_closed_order_has_no_card(self):
        cid, oid = self._order(delivered_days=8)
        self.assertEqual(rr.customer_cards(self.db, cid, [oid]), {})

    def test_assistant_sees_the_request_stage(self):
        cid, oid = self._order()
        self._submit(cid, oid)
        self.db.commit()
        self.assertEqual(ra.stage(self._case(oid), None), ra.ST_REQUESTED)
        self._decide(oid, rp.REQ_DECLINED, "The damage shown is from an impact.")
        self.db.commit()
        self.assertEqual(ra.stage(self._case(oid), None), ra.ST_NOT_APPROVED)
        model = {"orders": [{"order_id": oid, "stage": ra.ST_REQUESTED, "fee_state": "DUE"}]}
        self.assertIn(ra.V_PICKUP_BEFORE_FEE,
                      ra.reply_violations(model, "Your pickup has been booked for tomorrow."))

    def test_schema_is_additive_and_repeatable(self):
        rp._SCHEMA_READY = False
        rr._SCHEMA_READY = False
        rr.ensure_schema(self.db)
        rr.ensure_schema(self.db)
        self.cur.execute("SHOW COLUMNS FROM reverse_pickup_cases")
        cols = {r["Field"] for r in self.cur.fetchall()}
        for name, _ddl in rp.CASE_ADDED_COLUMNS:
            self.assertIn(name, cols)
        self.cur.execute("SELECT COUNT(*) AS n FROM policy_versions WHERE kind=%s AND version=%s",
                         (rr.POLICY_KIND, rr.DECLARATIONS_VERSION))
        self.assertEqual(self.cur.fetchone()["n"], 1)


def base_body():
    return {"operator": "ops-user@lensbazaar", "courier": "Delhivery", "awb": "36120510099999",
            "reference": "RP1", "reason": "Manufacturing defect",
            "pickup": {"name": "N S", "address": "x", "city": "Basti", "pin": "272001",
                       "phone": "9000000000"}}


if __name__ == "__main__":
    unittest.main()
