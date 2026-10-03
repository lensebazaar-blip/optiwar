"""The ₹250 reverse-pickup fee (phase 3b): an approved return is paid by its
signed-in owner on its own Razorpay order, applied once from Razorpay's own
record of the payment, and only then can Ops book the pickup."""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

import test_return_request as trr  # noqa: E402

base, rp, rr, ra, reship = trr.base, trr.rp, trr.rr, trr.ra, trr.reship
import importlib  # noqa: E402
rf = importlib.import_module(base.PKG + ".return_fee")
reship_api = trr.reship_api
RRT = trr.ReturnRequestTest


class ReturnFeeTest(unittest.TestCase):

    setUpClass = classmethod(RRT.setUpClass.__func__)
    tearDownClass = classmethod(RRT.tearDownClass.__func__)
    _order, _client, _form, _submit = RRT._order, RRT._client, RRT._form, RRT._submit
    _ops, _decide, _case = RRT._ops, RRT._decide, RRT._case

    def setUp(self):
        trr.ReturnRequestTest.setUp(self)
        base.Stubs.provider_orders = []
        base.Stubs.provider_payments = {}
        base.Stubs.signature_ok = True

    def tearDown(self):
        cur = self.db.cursor()
        for oid in self._orders:
            cur.execute("DELETE FROM payment_collector WHERE order_id=%s", (oid,))
        self.db.commit()
        trr.ReturnRequestTest.tearDown(self)

    # ------------------------------------------------------------ helpers

    def _approved(self):
        cid, oid = self._order()
        self.assertEqual(self._submit(cid, oid).status_code, 200)
        self.assertEqual(self._decide(oid, rp.REQ_APPROVED).status_code, 200)
        return cid, oid

    def _post(self, cid, oid, step, body=None, host="optiwar.in"):
        c = self._client(cid)
        return c.post("/api/orders/%s/return/payment/%s" % (oid, step), json=body or {},
                      headers={"Origin": "https://%s" % host},
                      environ_overrides={"HTTP_HOST": host})

    def _captured(self, rzp_order, pid="pay_RPF1", amount=25000, currency="INR", status="captured"):
        p = {"id": pid, "order_id": rzp_order, "amount": amount, "currency": currency,
             "status": status, "notes": {}}
        base.Stubs.provider_payments[pid] = p
        return p

    def _verify(self, cid, oid, rzp_order, pid="pay_RPF1"):
        return self._post(cid, oid, "verify", {"razorpay_payment_id": pid,
                                               "razorpay_order_id": rzp_order,
                                               "razorpay_signature": "sig"})

    def _outbox(self, oid):
        self.cur.execute("SELECT event FROM reverse_pickup_ops_outbox WHERE order_id=%s ORDER BY id",
                         (oid,))
        return [r["event"] for r in self.cur.fetchall()]

    def _fee_mails(self):
        return [m for m in self.mails if m[1] == "Optiwar Reverse-Pickup Fee Received"]

    # ------------------------------------------------------------ the payment

    def test_an_approved_return_is_paid_once_and_then_bookable(self):
        cid, oid = self._approved()
        r = self._ops(oid, "", trr.base_body())
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "fee_not_settled"))
        r = self._post(cid, oid, "create")
        self.assertEqual(r.status_code, 200, r.get_json())
        o = r.get_json()
        self.assertEqual((o["amount"], o["currency"], o["created"], o["order_id"]),
                         (25000, "INR", True, oid))
        again = self._post(cid, oid, "create").get_json()
        self.assertEqual((again["razorpay_order_id"], again["created"]),
                         (o["razorpay_order_id"], False))
        self.assertEqual(len(base.Stubs.provider_orders), 1)
        notes = base.Stubs.provider_orders[0]["notes"]
        self.assertEqual((notes["purpose"], notes["case_uuid"], notes["original_order_id"]),
                         (rf.PURPOSE, self._case(oid)["case_uuid"], oid))

        self._captured(o["razorpay_order_id"])
        r = self._verify(cid, oid, o["razorpay_order_id"])
        self.assertEqual(r.status_code, 200, r.get_json())
        case = self._case(oid)
        self.assertEqual((case["fee_state"], case["razorpay_payment_id"]), (rp.FEE_PAID, "pay_RPF1"))
        self.assertIsNotNone(case["fee_paid_at"])
        self.assertEqual(len(self._fee_mails()), 1)
        self.assertIn("pay_RPF1", self._fee_mails()[0][2])
        self.assertIn("₹250", self._fee_mails()[0][2])
        self.assertEqual(self._outbox(oid).count(rp.EV_FEE_PAID), 1)

        # A replayed callback is a duplicate: no second email, push or history.
        self.assertEqual(self._verify(cid, oid, o["razorpay_order_id"]).status_code, 200)
        self.assertEqual(len(self._fee_mails()), 1)
        self.assertEqual(self._outbox(oid).count(rp.EV_FEE_PAID), 1)
        r = self._post(cid, oid, "create")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "already_paid"))

        card = rr.customer_cards(self.db, cid, [oid])[oid]
        self.assertEqual((card["state"], card.get("fee_paid"), card.get("can_pay")),
                         (rr.CARD_APPROVED, True, None))
        self.assertTrue(rp.state_view(self.db, oid)["booking_allowed"])
        r = self._ops(oid, "", trr.base_body())
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_no_payment_before_approval_after_decline_or_when_waived(self):
        cid, oid = self._order()
        self._submit(cid, oid)
        r = self._post(cid, oid, "create")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "not_approved"))
        self._decide(oid, rp.REQ_INFO, "Please send a clearer photo.")
        r = self._post(cid, oid, "create")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "not_approved"))
        self._decide(oid, rp.REQ_DECLINED, "Not a manufacturing fault.")
        r = self._post(cid, oid, "create")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "not_approved"))

        cid2, oid2 = self._order()
        self._submit(cid2, oid2)
        self._ops(oid2, "/fee/waive", {"reason_code": "GOODWILL", "operator": "ops"})
        self._decide(oid2, rp.REQ_APPROVED)
        r = self._post(cid2, oid2, "create")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "fee_waived"))
        card = rr.customer_cards(self.db, cid2, [oid2])[oid2]
        self.assertEqual((card["state"], card.get("can_pay"), card.get("fee_paid")),
                         (rr.CARD_APPROVED, None, False))
        self.assertEqual(base.Stubs.provider_orders, [])

    def test_only_the_signed_in_owner_on_the_india_site(self):
        cid, oid = self._approved()
        other, _ = self._order()
        self.assertEqual(self._post(None, oid, "create").status_code, 401)
        self.assertEqual(self._post(other, oid, "create").status_code, 404)
        self.assertEqual(self._post(cid, oid, "create", host="optiwar.com").status_code, 404)
        self.assertEqual(self._post(other, oid, "verify", {"razorpay_payment_id": "p",
                                                          "razorpay_order_id": "o",
                                                          "razorpay_signature": "s"}).status_code,
                         404)
        os.environ[rp.CUSTOMER_ENV] = "false"
        self.assertEqual(self._post(cid, oid, "create").status_code, 404)
        self.assertEqual(base.Stubs.provider_orders, [])

    def test_a_payment_that_does_not_match_is_refused_and_audited(self):
        cid, oid = self._approved()
        rzp = self._post(cid, oid, "create").get_json()["razorpay_order_id"]
        base.Stubs.signature_ok = False
        self._captured(rzp)
        self.assertEqual(self._verify(cid, oid, rzp).status_code, 400)
        base.Stubs.signature_ok = True
        self.assertEqual(self._verify(cid, oid, "order_OTHER").status_code, 400)
        self._captured("order_OTHER", pid="pay_X")
        self.assertEqual(self._verify(cid, oid, rzp, pid="pay_X").status_code, 400)
        self._captured(rzp, pid="pay_LOW", amount=100)
        self.assertEqual(self._verify(cid, oid, rzp, pid="pay_LOW").status_code, 400)
        self._captured(rzp, pid="pay_USD", currency="USD")
        self.assertEqual(self._verify(cid, oid, rzp, pid="pay_USD").status_code, 400)
        self._captured(rzp, pid="pay_AUTH", status="authorized")
        self.assertEqual(self._verify(cid, oid, rzp, pid="pay_AUTH").status_code, 202)
        case = self._case(oid)
        self.assertEqual((case["fee_state"], case["razorpay_payment_id"]), (rp.FEE_DUE, None))
        self.cur.execute("SELECT payload FROM reship_events WHERE order_id=%s AND event_type=%s",
                         (oid, rf.EV_PAYMENT_REFUSED))
        reasons = [r["payload"] for r in self.cur.fetchall()]
        self.assertEqual(len(reasons), 2)
        self.assertTrue(any(rf.AMOUNT_MISMATCH in p for p in reasons))
        self.assertTrue(any(rf.CURRENCY_MISMATCH in p for p in reasons))
        self.assertEqual(self._fee_mails(), [])

    def test_a_payment_bound_elsewhere_is_refused(self):
        cid, oid = self._approved()
        rzp = self._post(cid, oid, "create").get_json()["razorpay_order_id"]
        p = self._captured(rzp, pid="pay_TAKEN")
        self.cur.execute("INSERT INTO payment_collector (order_id, payment_ref, payment_dump, status) "
                         "VALUES (%s,'pay_TAKEN','{}','TXN_SUCCESS')", (oid,))
        self.db.commit()
        res = rf.settle(self.db, self._case(oid)["case_uuid"], p, "test")
        self.assertEqual(res["outcome"], rf.ALREADY_BOUND)

        cid2, oid2 = self._approved()
        rzp2 = self._post(cid2, oid2, "create").get_json()["razorpay_order_id"]
        self._captured(rzp2, pid="pay_ONE")
        self.assertEqual(self._verify(cid2, oid2, rzp2, pid="pay_ONE").status_code, 200)
        stolen = dict(base.Stubs.provider_payments["pay_ONE"], order_id=rzp)
        res = rf.settle(self.db, self._case(oid)["case_uuid"], stolen, "test")
        self.assertEqual(res["outcome"], rf.ALREADY_BOUND)
        self.assertEqual(self._case(oid)["fee_state"], rp.FEE_DUE)

    def test_a_capture_after_the_fee_was_waived_is_an_exception_not_a_payment(self):
        cid, oid = self._approved()
        rzp = self._post(cid, oid, "create").get_json()["razorpay_order_id"]
        self._ops(oid, "/fee/waive", {"reason_code": "GOODWILL", "operator": "ops"})
        res = rf.settle(self.db, self._case(oid)["case_uuid"], self._captured(rzp), "test")
        self.assertEqual(res["outcome"], rf.NOT_PAYABLE)
        self.assertEqual(self._case(oid)["fee_state"], rp.FEE_WAIVED)
        self.cur.execute("SELECT order_history_content AS c FROM order_history WHERE order_id=%s",
                         (oid,))
        self.assertTrue(any("PAYMENT EXCEPTION" in r["c"] for r in self.cur.fetchall()))

    # ------------------------------------------------------------ webhook and reconcile

    def test_the_webhook_resolves_the_case_by_razorpay_order(self):
        cid, oid = self._approved()
        rzp = self._post(cid, oid, "create").get_json()["razorpay_order_id"]
        uuid = self._case(oid)["case_uuid"]
        self.assertEqual(rf.case_for_payment(self.db, {"order_id": rzp}), uuid)
        self.assertEqual(rf.case_for_payment(self.db, {"order_id": "order_MERCH"}), "")
        self.assertEqual(rf.case_for_payment(
            self.db, {"order_id": "order_X", "notes": {"purpose": rf.PURPOSE, "case_uuid": uuid}}),
            uuid)
        with self.app.test_request_context(base_url="https://optiwar.in/"):
            res = reship_api.rp_fee_settle_and_notify(self.db, uuid, self._captured(rzp),
                                                      "razorpay-webhook")
            self.assertEqual(res["outcome"], rf.APPLIED)
            res = reship_api.rp_fee_settle_and_notify(self.db, uuid, self._captured(rzp),
                                                      "razorpay-webhook")
            self.assertEqual(res["outcome"], rf.DUPLICATE)
        self.assertEqual(len(self._fee_mails()), 1)

    def test_reconcile_applies_a_capture_the_browser_never_reported(self):
        cid, oid = self._approved()
        rzp = self._post(cid, oid, "create").get_json()["razorpay_order_id"]
        cid2, oid2 = self._approved()
        self._post(cid2, oid2, "create")
        p = self._captured(rzp, pid="pay_LATE")
        by_order = {rzp: [p]}
        summary = rf.reconcile_pending(self.db, lambda o: by_order.get(o, []))
        self.assertIn(self._case(oid)["case_uuid"], summary["settled"])
        self.assertGreaterEqual(summary["unpaid"], 1)
        self.assertEqual(self._case(oid)["fee_state"], rp.FEE_PAID)
        self.assertEqual(self._case(oid2)["fee_state"], rp.FEE_DUE)
        again = rf.reconcile_pending(self.db, lambda o: by_order.get(o, []))
        self.assertNotIn(self._case(oid)["case_uuid"], again["settled"])
        # The notice owed in the settling transaction is sent by the retry job.
        self.assertEqual(self._fee_mails(), [])
        rp.retry_notices(self.db)
        self.assertEqual(len(self._fee_mails()), 1)
        rp.retry_notices(self.db)
        self.assertEqual(len(self._fee_mails()), 1)

    # ------------------------------------------------------------ customer surfaces

    def test_card_offers_pay_only_while_the_fee_is_due(self):
        cid, oid = self._approved()
        card = rr.customer_cards(self.db, cid, [oid])[oid]
        self.assertEqual((card["state"], card["can_pay"]), (rr.CARD_APPROVED_FEE_DUE, True))
        model = ra.read_model(self.db, cid, "optiwar.in")
        entry = [e for e in model["orders"] if e["order_id"] == oid][0]
        self.assertTrue(entry["pay_in_my_orders"])
        for key in ("razorpay_order_id", "razorpay_payment_id"):
            self.assertNotIn(key, card)


del RRT


if __name__ == "__main__":
    unittest.main()
