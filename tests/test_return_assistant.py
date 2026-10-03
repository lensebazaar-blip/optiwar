"""The assistant's knowledge of a customer's return is the return record's,
driven here through the real Ops API against a test database: the fee gates
the pickup, a pickup AWB is told only while it is booked, receipt, inspection
and the customer's reply each change what may be said, nothing Ops typed
reaches the model, and another customer's return is never in the model."""
import importlib
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

import test_reverse_pickup as base  # noqa: E402

PKG = base.PKG
rp = base.rp
ra = importlib.import_module(PKG + ".return_assistant")
IN_HOST = "optiwar.in"


class _Harness(base.ReversePickupTest):
    """The reverse-pickup fixtures without re-running its tests here."""
    locals().update({n: None for n in dir(base.ReversePickupTest) if n.startswith("test_")})


class ReturnAssistantTest(_Harness):

    def _model(self, cid, host=IN_HOST, env=None):
        return ra.read_model(self.db, cid, host, environ=dict(os.environ) if env is None else env)

    def _cid(self, oid):
        self.cur.execute("SELECT customer_id FROM orders WHERE order_id=%s", (oid,))
        return self.cur.fetchone()["customer_id"]

    def _only(self, model):
        self.assertEqual(len(model["orders"]), 1, model)
        return model["orders"][0]

    # ------------------------------------------------------------- stages

    def test_a_due_fee_has_no_pickup_and_the_model_is_told_so(self):
        cid, oid = self._order(fee="DUE")
        m = self._model(cid)
        e = self._only(m)
        self.assertEqual((e["stage"], e["fee_state"], e["pickup_awb"]), (ra.ST_FEE_DUE, "DUE", None))
        self.assertEqual(ra.get_reverse_pickup_tracking(m, oid)["booked"], False)
        fee = ra.get_return_fee_status(m, oid)
        self.assertEqual((fee["fee"], fee["settled"]), (250, False))
        text = ra.prompt_section(m)
        self.assertIn("stage=FEE_DUE", text)
        self.assertIn("INR 250 fee due before a pickup can be booked", text)
        self.assertEqual(ra.reply_violations(m, "Your reverse pickup has been booked for tomorrow."),
                         [ra.V_PICKUP_BEFORE_FEE])
        self.assertEqual(ra.reply_violations(
            m, "The pickup is not booked yet; it is booked once the INR 250 fee is settled."), [])

    def test_a_settled_fee_waits_for_ops_to_book(self):
        cid, oid = self._order(fee="PAID")
        e = self._only(self._model(cid))
        self.assertEqual(e["stage"], ra.ST_TO_BOOK)
        self.assertIn("fee settled; our team books the pickup; no AWB yet",
                      ra.prompt_section(self._model(cid)))

    def test_a_booked_pickup_gives_its_awb_and_tracking_and_a_cancel_withdraws_them(self):
        oid, awb = self._booked("PAID")
        cid = self._cid(oid)
        m = self._model(cid)
        t = ra.get_reverse_pickup_tracking(m, "OW-" + oid)
        self.assertEqual((t["booked"], t["awb"], t["courier"]), (True, awb, "Delhivery"))
        self.assertTrue(t["track_url"].startswith("https://"))
        self.assertNotIn("evil.example", str(m))
        text = ra.prompt_section(m)
        self.assertIn("pickup_awb=%s (Delhivery)" % awb, text)
        self.assertEqual(ra.reply_violations(m, "Your pickup AWB is %s." % awb), [])
        self.assertEqual(ra.reply_violations(m, "Your pickup AWB is 36129999999999."),
                         [ra.V_AWB_INVENTED])
        self.assertEqual(ra.reply_violations(m, "Your original AWB was %s." % base.FORWARD), [])

        self.assertEqual(self._post(oid, {"awb": awb}, path="/cancel").status_code, 200)
        m = self._model(cid)
        t = ra.get_reverse_pickup_tracking(m, oid)
        self.assertEqual((t["booked"], t["cancelled"], t["awb"]), (False, True, None))
        self.assertIn("stage=PICKUP_CANCELLED", ra.prompt_section(m))
        self.assertNotIn("pickup_awb=", ra.prompt_section(m))

    def test_receipt_then_no_defect_asks_for_the_customers_reply_and_promises_no_refund(self):
        oid, awb = self._booked("PAID")
        cid = self._cid(oid)
        self._received(oid, awb, notes="box dented by courier")
        m = self._model(cid)
        self.assertEqual(self._only(m)["stage"], ra.ST_RECEIVED)
        self.assertTrue(ra.get_return_case_status(m, oid)["received"])
        self.assertIn("received_by_optiwar=", ra.prompt_section(m))

        self._post(oid, {"operator": "qc", "manufacturing_defect": False,
                         "remarks": "lens intact QC-SECRET-REMARK"}, path="/inspection")
        m = self._model(cid)
        i = ra.get_return_inspection_status(m, oid)
        self.assertEqual((i["inspected"], i["manufacturing_defect"], i["awaiting_customer_reply"]),
                         (True, False, True))
        text = ra.prompt_section(m)
        self.assertIn("stage=INSPECTED_NO_DEFECT", text)
        self.assertIn("manufacturing_defect=not confirmed", text)
        for typed in ("QC-SECRET-REMARK", "box dented", "ops-user", "qc"):
            self.assertNotIn(typed, text.split("CUSTOMER'S RETURNS")[1])
        self.assertEqual(ra.reply_violations(m, "Your INR 250 will be refunded in 5 days."),
                         [ra.V_REFUND_PROMISED])
        self.assertEqual(ra.reply_violations(m, "The fee will not be refunded, as the defect "
                                                "was not confirmed."), [])

        self._post(oid, {"message_id": "<r@mail>"}, path="/consent")
        m = self._model(cid)
        self.assertEqual(self._only(m)["stage"], ra.ST_CONSENT)
        self.assertTrue(ra.get_return_inspection_status(m, oid)["customer_reply_recorded"])
        self.assertIn("customer_reply_recorded=", ra.prompt_section(m))
        self.assertNotIn("<r@mail>", ra.prompt_section(m))

    def test_a_confirmed_defect(self):
        oid, awb = self._booked("WAIVED")
        self._received(oid, awb)
        self._post(oid, {"manufacturing_defect": True}, path="/inspection")
        m = self._model(self._cid(oid))
        e = self._only(m)
        self.assertEqual((e["stage"], e["manufacturing_defect"], e["fee_state"]),
                         (ra.ST_DEFECT, True, "WAIVED"))
        self.assertFalse(ra.get_return_inspection_status(m, oid)["awaiting_customer_reply"])

    def test_a_refunded_fee_is_stated_with_its_amount(self):
        cid, oid = self._order(fee="REFUNDED")
        self.cur.execute("UPDATE reverse_pickup_cases SET fee_refunded_minor=25000 WHERE order_id=%s",
                         (oid,))
        self.db.commit()
        m = self._model(cid)
        self.assertEqual(ra.get_return_fee_status(m, oid)["refunded"], 250)
        self.assertIn("fee_refunded=INR 250", ra.prompt_section(m))
        self.assertEqual(ra.reply_violations(m, "Your fee has been refunded; you will get a refund "
                                                "credit in your account."), [])

    def test_a_pickup_booked_before_the_fee_flow_is_still_shown(self):
        cid, oid = self._order(fee=None)
        self._legacy_pickup(oid)
        e = self._only(self._model(cid))
        self.assertEqual((e["stage"], e["fee_state"], e["pickup_awb"]), (ra.ST_BOOKED, None, base.AWB))
        self.assertIn("fee_state=not recorded", ra.prompt_section(self._model(cid)))

    # ------------------------------------------------------------- scoping

    def test_only_the_customers_own_india_orders_with_a_return(self):
        cid, oid = self._order(fee="DUE")
        other, other_oid = self._order(fee="PAID")
        self.assertEqual([e["order_id"] for e in self._model(cid)["orders"]], [oid])
        self.assertIsNone(ra.get_return_case_status(self._model(cid), other_oid))
        plain_cid, _plain = self._order(fee=None)
        m = self._model(plain_cid)
        self.assertEqual(m["orders"], [])
        self.assertIn("none on record", ra.prompt_section(m))
        self.assertEqual(ra.reply_violations(m, "Your refund will be processed."), [])

    def test_a_test_order_is_not_a_customers_return(self):
        cid, oid = self._order(fee="DUE")
        self.cur.execute("UPDATE orders SET is_test=1 WHERE order_id=%s", (oid,))
        self.db.commit()
        self.assertEqual(self._model(cid)["orders"], [])

    def test_closed_where_customers_are_not_told(self):
        cid, oid = self._order(fee="DUE")
        env = dict(os.environ)
        self.assertEqual(len(self._model(cid, env=env)["orders"]), 1)
        self.assertEqual(self._model(cid, host="optiwar.com", env=env)["orders"], [])
        for flag in (rp.ENABLED_ENV, rp.CUSTOMER_ENV):
            off = dict(env)
            off.pop(flag)
            self.assertFalse(ra.enabled(IN_HOST, off))
            self.assertEqual(self._model(cid, env=off)["orders"], [])
        self.assertEqual(self._model(None)["orders"], [])

    # ------------------------------------------------------------- KET

    def test_ket_snapshot_carries_stages_and_awbs_only(self):
        oid, awb = self._booked("PAID")
        self._received(oid, awb, notes="NOTE-TYPED-BY-OPS")
        m = self._model(self._cid(oid))
        ctx = ra.ket_context(m)
        self.assertEqual(len(ctx), 1)
        self.assertEqual((ctx[0]["order_id"], ctx[0]["stage"], ctx[0]["pickup_awb"]),
                         (oid, ra.ST_RECEIVED, awb))
        text = ra.ket_context_text(ctx)
        self.assertIn("Return / reverse-pickup context", text)
        self.assertIn("stage=RECEIVED_AWAITING_INSPECTION", text)
        self.assertNotIn("NOTE-TYPED-BY-OPS", text)
        self.assertNotIn("ops-user", text)
        self.assertEqual(ra.ket_context_text([]), "")


class GatewayWiringTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "chat_gateway.py"), encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_the_customer_is_the_browsers_login(self):
        body = self.src[self.src.index("def _return_model("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("flask_session.get('user_id')", body)
        self.assertIn("dev_defects.record('CHAT_RETURN_CONTEXT_UNAVAILABLE'", body)

    def test_the_section_is_in_the_prompt_the_snapshot_in_tickets_and_a_breach_is_logged(self):
        self.assertIn("reship_section += return_assistant.prompt_section(return_model)", self.src)
        self.assertIn("return_assistant.ket_context(return_model)) if return_model else ''", self.src)
        self.assertIn("acr.log_event(db, acr.EV_RETURN_RULE_BREACH", self.src)

    def test_deployable(self):
        with open(os.path.join(REPO, "deploy", "deploy.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().count('"return_assistant.py"'), 2)


class GatewayFunctionalTests(_Harness):
    """The real ``_return_model`` in a Flask request."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from flask import Flask
        import test_ai_wrapper  # loads flaskr.chat_gateway with its stubs
        cls.cg = test_ai_wrapper.cg
        cls.flask_app = Flask(__name__)
        cls.flask_app.config.update(TESTING=True, SECRET_KEY="t")

    def _call(self, user_id, url="https://optiwar.in/profile/"):
        ctx = self.flask_app.test_request_context("/api/chat/message")
        ctx.push()
        try:
            from flask import session
            if user_id:
                session["user_id"] = user_id
            return self.cg._return_model(self.db, url)
        finally:
            ctx.pop()

    def test_signed_in_customer_gets_their_returns_and_nobody_elses(self):
        cid, oid = self._order(fee="DUE")
        other, _o = self._order(fee=None)
        self.assertEqual([e["order_id"] for e in self._call(cid)["orders"]], [oid])
        self.assertEqual(self._call(other)["orders"], [])
        self.assertIsNone(self._call(None))
        self.assertIsNone(self._call(cid, "https://optiwar.com/profile/"))

    def test_a_record_read_failure_is_none_not_a_guess(self):
        cid, _oid = self._order(fee="DUE")
        live = sys.modules["flaskr.return_assistant"]
        keep = live.read_model

        def boom(*a, **k):
            raise RuntimeError("record away")
        live.read_model = boom
        try:
            with self.flask_app.app_context():
                self.assertIsNone(self._call(cid))
        finally:
            live.read_model = keep


if __name__ == "__main__":
    unittest.main()
