"""The assistant's returned-parcel knowledge is the ledger's, read through the
same projection as My Orders, against a real test database.

RETURNING_TO_OPS is not payable; RETURNED_TO_OPS is; a paid parcel has no
deadline; the AWB appears only once Ops has shipped; an abandoned parcel is
referred to support; an order outside the workflow has no record at all —
and the model is told each of these, never left to infer them.
"""
import os
import sys
import unittest
from datetime import timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

import test_reship as base  # noqa: E402  (the name discover loads it under)

PKG = base.PKG
reship = base.reship
ra = base._load("reship_assistant")
IN_HOST = "optiwar.in"


class _Ledger(base.ReshipTest):
    """The reship harness's fixtures (customers, orders, ledger rows, clock)
    without re-running its own tests under this module."""
    locals().update({n: None for n in dir(base.ReshipTest) if n.startswith("test_")})


class ReshipAssistantTest(_Ledger):

    def _model(self, cid, host=IN_HOST, now=None):
        return ra.read_model(self.db, cid, host, environ=dict(os.environ), now=now)

    def _only(self, model):
        self.assertEqual(len(model["orders"]), 1, model)
        return model["orders"][0]

    # ------------------------------------------------------------- states

    def test_courier_returned_is_returning_and_not_payable(self):
        cid = self._customer()
        oid = self._order(cid)
        m = self._model(cid)
        e = self._only(m)
        self.assertEqual((e["state"], e["can_pay"]), (ra.STATE_RETURNING, False))
        st = ra.get_order_reship_status(m, oid)
        self.assertEqual((st["state"], st["can_pay"]), ("RETURNING_TO_OPS", False))
        ret = ra.get_return_status(m, oid)
        self.assertFalse(ret["physically_received"])
        self.assertEqual(ret["original_awb"], "7X119057819")
        pay = ra.get_reship_payment_status(m, oid)
        self.assertEqual((pay["paid"], pay["can_pay"], pay["pay_at"]), (False, False, None))
        self.assertIsNone(ra.get_reship_holding_deadline(m, oid)["abandon_at"])
        text = ra.prompt_section(m)
        self.assertIn("state=RETURNING_TO_OPS", text)
        self.assertIn("can_pay=no", text)
        self.assertIn("not received by Optiwar yet", text)
        self.assertNotIn("abandon_at=", text)

    def test_physical_receipt_makes_it_payable_with_the_servers_deadline(self):
        cid, oid, row = self._returned()
        m = self._model(cid)
        e = self._only(m)
        self.assertEqual((e["state"], e["sub_state"], e["can_pay"]),
                         (ra.STATE_RETURNED, ra.SUB_AVAILABLE, True))
        self.assertEqual(ra.get_reship_payment_status(m, oid)["pay_at"], ra.MY_ORDERS_PATH)
        d = ra.get_reship_holding_deadline(m, oid)
        self.assertEqual(d["abandon_at"], row["abandon_at"])
        self.assertEqual(d["days_remaining"], reship.holding(row)["days_remaining"])
        self.assertTrue(d["will_be_abandoned"])
        text = ra.prompt_section(m)
        self.assertIn("state=RETURNED_TO_OPS", text)
        self.assertIn("can_pay=yes", text)
        self.assertIn("abandon_at=%s" % row["abandon_at"].strftime("%d %b %Y"), text)
        self.assertIn("days_remaining=", text)
        self.assertIn("INR 250", text)

    def test_payment_started_is_awaiting_payment(self):
        cid, oid, row = self._returned()
        self._start(cid, row)
        m = self._model(cid)
        self.assertEqual(self._only(m)["sub_state"], ra.SUB_AWAITING)
        self.assertTrue(self._only(m)["can_pay"])

    def test_final_window_is_named_and_day_59_payment_is_never_abandoned(self):
        cid, oid, row = self._returned()
        late = self._at(row, 57)
        m = self._model(cid, now=late)
        e = self._only(m)
        self.assertEqual((e["sub_state"], e["final_period"]), (ra.SUB_APPROACHING, True))
        self.assertEqual(ra.get_reship_holding_deadline(m, oid)["days_remaining"], 3)
        # pays on day 59
        self._start(cid, row)
        pay = base._payment("pay_d59", reship.by_uuid(self.db, row["reship_uuid"])["razorpay_order_id"])
        self.assertEqual(reship.settle_payment(self.db, row["reship_uuid"], pay, "test")["outcome"],
                         reship.APPLIED)
        m = self._model(cid, now=self._at(row, 61))
        e = self._only(m)
        self.assertEqual((e["state"], e["can_pay"]), (ra.STATE_PAID, False))
        d = ra.get_reship_holding_deadline(m, oid)
        self.assertEqual((d["abandon_at"], d["days_remaining"], d["will_be_abandoned"],
                          d["abandoned"]), (None, None, False, False))
        p = ra.get_reship_payment_status(m, oid)
        self.assertEqual((p["paid"], p["can_pay"]), (True, False))
        self.assertIsNotNone(p["paid_at"])
        text = ra.prompt_section(m)
        self.assertIn("fee_paid=yes", text)
        self.assertIn("a paid parcel is never abandoned", text)
        self.assertIn("no new AWB yet", text)
        self.assertNotIn("abandon_at=", text)

    def test_new_awb_only_once_reshipped(self):
        cid, oid, row, _ = self._paid()
        m = self._model(cid)
        t = ra.get_reship_tracking(m, oid)
        self.assertEqual((t["reshipped"], t["new_awb"]), (False, None))
        self.assertNotIn("new_awb=", ra.prompt_section(m))
        reship.ship(self.db, row["reship_uuid"], "ops", "7X119057999", "DTDC")
        m = self._model(cid)
        e = self._only(m)
        self.assertEqual(e["state"], ra.STATE_RESHIPPED)
        t = ra.get_reship_tracking(m, oid)
        self.assertEqual((t["reshipped"], t["new_awb"], t["new_courier"]),
                         (True, "7X119057999", "DTDC"))
        self.assertIsNotNone(t["reshipped_at"])
        text = ra.prompt_section(m)
        self.assertIn("new_awb=7X119057999 (DTDC)", text)
        self.assertIn("state=RESHIPPED", text)

    def test_held_parcel_is_not_offered_payment(self):
        cid, oid, row = self._returned()
        reship.hold(self.db, row["reship_uuid"], "ravi", "legal hold")
        m = self._model(cid)
        e = self._only(m)
        self.assertEqual((e["sub_state"], e["can_pay"], e["on_hold"]), (ra.SUB_HELD, False, True))
        self.assertIn("do not offer payment", ra.prompt_section(m))

    def test_abandoned_is_said_plainly_and_referred_to_support(self):
        cid, oid, row = self._returned()
        self._sweep(self._at(row, 60))
        m = self._model(cid)
        e = self._only(m)
        self.assertEqual((e["state"], e["can_pay"]), (ra.STATE_ABANDONED, False))
        d = ra.get_reship_holding_deadline(m, oid)
        self.assertTrue(d["abandoned"])
        self.assertIsNotNone(d["abandoned_at"])
        self.assertEqual(ra.get_reship_tracking(m, oid)["reshipped"], False)
        text = ra.prompt_section(m)
        self.assertIn("state=ABANDONED", text)
        self.assertIn("refer to support@optiwar.com", text)

    # ------------------------------------------------------------- absence

    def test_an_order_with_no_record_is_none_not_a_guess(self):
        cid = self._customer()
        m = self._model(cid)
        self.assertEqual(m["orders"], [])
        for fn in (ra.get_order_reship_status, ra.get_return_status,
                   ra.get_reship_payment_status, ra.get_reship_tracking,
                   ra.get_reship_holding_deadline):
            self.assertIsNone(fn(m, "NOSUCH-000001"))
        self.assertIn("none on record", ra.prompt_section(m))

    def test_lookup_is_case_insensitive_and_scoped_to_the_customer(self):
        cid, oid, row = self._returned()
        other = self._customer("o@example.in")
        m = self._model(cid)
        self.assertIsNotNone(ra.get_order_reship_status(m, oid.lower()))
        self.assertEqual(self._model(other)["orders"], [])

    def test_closed_workflow_has_no_section(self):
        cid, oid, row = self._returned()
        self.assertFalse(ra.enabled("optiwar.com", dict(os.environ)))
        self.assertEqual(self._model(cid, host="optiwar.com")["orders"], [])
        env = dict(os.environ)
        env[reship.ENABLED_ENV] = "false"
        self.assertFalse(ra.enabled(IN_HOST, env))
        self.assertEqual(ra.read_model(self.db, cid, IN_HOST, environ=env)["orders"], [])
        self.assertEqual(ra.read_model(self.db, None, IN_HOST)["orders"], [])

    def test_a_test_order_is_not_a_returned_parcel(self):
        cid, oid, row = self._returned()
        self.cur.execute("UPDATE orders SET is_test=1 WHERE order_id=%s", (oid,))
        self.db.commit()
        self.assertEqual(self._model(cid)["orders"], [])

    # ------------------------------------------------------------- rules

    def test_rules_carry_the_fee_holding_days_and_the_navigation_action(self):
        cid = self._customer()
        text = ra.prompt_section(self._model(cid))
        self.assertIn("INR 250", text)
        self.assertIn("held %d days" % reship.abandon_days(dict(os.environ)), text)
        self.assertIn("[ACTION:NAVIGATE:/profile/?tab=orders]", text)
        self.assertIn("NOT say they can pay yet", text)
        self.assertIn("never ask the customer to pay again", text)
        self.assertIn("Never invent an AWB", text)
        self.assertIn("cannot extend a deadline", text)

    # ------------------------------------------------------------- KET

    def test_ket_context_is_the_ledger_snapshot_without_provider_ids(self):
        cid, oid, row, pay = self._paid()
        m = self._model(cid)
        ctx = ra.ket_context(self.db, m)
        self.assertEqual(len(ctx), 1)
        c = ctx[0]
        self.assertEqual((c["order_id"], c["state"], c["fee_paid"]), (oid, "RESHIP_PAID", True))
        self.assertEqual(c["reship_uuid"], row["reship_uuid"])
        self.assertEqual(c["original_awb"], "7X119057819")
        self.assertIsNone(c["new_awb"])
        self.assertEqual(set(c), set(ra.KET_FIELDS))
        text = ra.ket_context_text(ctx)
        self.assertIn("Returned-parcel context (snapshot at escalation):", text)
        self.assertIn("order_id=%s" % oid, text)
        self.assertIn("state=RESHIP_PAID", text)
        self.assertIn("fee_paid=True", text)
        for secret in (pay["id"], "order_RS", "razorpay", "Bearer", "token", "http"):
            self.assertNotIn(secret, text)
        self.assertEqual(ra.ket_context_text([]), "")

    def test_ket_context_survives_a_notification_read_failure(self):
        cid, oid, row = self._returned()
        m = self._model(cid)
        keep = reship.notifications_for

        def boom(*a, **k):
            raise RuntimeError("ledger away")
        reship.notifications_for = boom
        try:
            ctx = ra.ket_context(self.db, m)
        finally:
            reship.notifications_for = keep
        self.assertEqual(ctx[0]["order_id"], oid)
        self.assertIsNone(ctx[0]["notified"])


class GatewayWiringTests(unittest.TestCase):
    """The chat gateway reaches the module in the right places, by source."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "chat_gateway.py"), encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_the_customer_is_the_browsers_login(self):
        body = self.src[self.src.index("def _reship_model("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("flask_session.get('user_id')", body)
        self.assertNotIn("customer_id')", body.replace("flask_session.get('user_id')", ""))
        self.assertIn("dev_defects.record('CHAT_RESHIP_CONTEXT_UNAVAILABLE'", body)

    def test_the_section_is_in_the_prompt_and_the_snapshot_in_both_tickets(self):
        src = self.src
        self.assertIn("reship_section) if s))", src)
        self.assertIn("reship_assistant.ket_context(db, reship_model)) if reship_model else ''", src)
        self.assertEqual(src.count("{reship_note}"), 2)
        self.assertIn("acr.log_event(db, acr.EV_RESHIP_RULE_BREACH", src)

    def test_deployable(self):
        with open(os.path.join(REPO, "deploy", "deploy.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().count('"reship_assistant.py"'), 2)


class GatewayFunctionalTests(_Ledger):
    """The real ``_reship_model`` in a Flask request: the signed-in customer's
    ledger facts, nobody's when signed out, and a breach is a canonical event."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from flask import Flask
        import test_ai_wrapper  # loads flaskr.chat_gateway with its stubs
        cls.cg = test_ai_wrapper.cg
        cls.acr = sys.modules["flaskr.acr"]
        cls.acr.ensure_schema(lambda: base._connect())
        cls.app = Flask(__name__)
        cls.app.config.update(TESTING=True, SECRET_KEY="t")

    def _ctx(self, user_id):
        ctx = self.app.test_request_context("/api/chat/message")
        ctx.push()
        from flask import session
        if user_id:
            session["user_id"] = user_id
        return ctx

    def test_signed_in_customer_gets_their_ledger_and_nobody_elses(self):
        cid, oid, row = self._returned()
        other = self._customer("other@example.in")
        for uid, expect in ((cid, 1), (other, 0), (None, None)):
            ctx = self._ctx(uid)
            try:
                m = self.cg._reship_model(self.db, "https://optiwar.in/profile/")
            finally:
                ctx.pop()
            if expect is None:
                self.assertIsNone(m)
            else:
                self.assertEqual(len(m["orders"]), expect, uid)
        ctx = self._ctx(cid)
        try:
            self.assertIsNone(self.cg._reship_model(self.db, "https://optiwar.com/profile/"))
        finally:
            ctx.pop()

    def test_a_ledger_read_failure_is_none_not_a_guess(self):
        cid, oid, row = self._returned()
        ra_live = sys.modules["flaskr.reship_assistant"]
        keep = ra_live.read_model

        def boom(*a, **k):
            raise RuntimeError("ledger away")
        ra_live.read_model = boom
        ctx = self._ctx(cid)
        try:
            with self.app.app_context():
                self.assertIsNone(self.cg._reship_model(self.db, "https://optiwar.in/"))
        finally:
            ra_live.read_model = keep
            ctx.pop()

    def test_a_breach_is_a_canonical_event_without_the_reply(self):
        cid, oid, row = self._returned()
        sid = "reship_breach_" + row["reship_uuid"][:8]
        ra_live = sys.modules["flaskr.reship_assistant"]
        m = ra_live.read_model(self.db, cid, IN_HOST)
        codes = ra_live.reply_violations(m, "Your new AWB is 7X55550000, track it online.")
        self.assertEqual(codes, [ra_live.V_AWB_INVENTED])
        self.acr.log_event(self.db, self.acr.EV_RESHIP_RULE_BREACH, session_id=sid,
                           journey_stage=self.acr.STAGE_SUPPORT, page_url="/x",
                           payload={"codes": codes})
        cur = self.db.cursor()
        cur.execute("SELECT event_type, payload FROM ai_events WHERE session_id=%s", (sid,))
        rows = cur.fetchall()
        cur.execute("DELETE FROM ai_events WHERE session_id=%s", (sid,))
        self.db.commit()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "RESHIP_RULE_BREACH")
        self.assertIn("RESHIP_AWB_NOT_IN_LEDGER", str(rows[0]["payload"]))
        self.assertNotIn("7X55550000", str(rows[0]["payload"]))


if __name__ == "__main__":
    unittest.main()
