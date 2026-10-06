"""PR C: "Did this answer your question?" is asked once, at a terminal support
answer read from the customer's own record, never while shopping; the answer
is a SATISFACTION_RECORDED event, and a no gets one recovery answer that ends
by offering a ticket. ``OnMariaDB`` drives ``/api/chat/message`` with a
scripted model and is skipped without the CI MariaDB."""
import json
import os
import sys
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from reports import ai_chats_section as acs  # noqa: E402
from tests.test_ai_chats_section import _ev  # noqa: E402
from tests.test_chat_attachments import (  # noqa: E402
    AVAILABLE, CHAT_EVENTS_DDL, CHAT_MESSAGES_DDL, CHAT_SESSIONS_DDL,
    _connect, _load_gateway)


def _sat():
    cg = sys.modules.get("flaskr.chat_gateway") or _load_gateway()
    return cg.satisfaction, cg


class Unit(unittest.TestCase):

    def test_only_a_terminal_support_answer_from_the_account_is_asked_once(self):
        sat, _ = _sat()
        self.assertTrue(sat.should_ask("ORDER_STATUS", True, True, False, False))
        self.assertTrue(sat.should_ask("PRESCRIPTION_STATUS", True, True, False, False))
        self.assertTrue(sat.should_ask("RESHIP_STATUS", True, True, False, False))
        self.assertFalse(sat.should_ask("PRODUCT_SEARCH", True, True, False, False))
        self.assertFalse(sat.should_ask("PRESCRIPTION_ENTRY_HELP", True, True, False, False))
        self.assertFalse(sat.should_ask("ORDER_STATUS", False, True, False, False))
        self.assertFalse(sat.should_ask("ORDER_STATUS", True, False, False, False))
        self.assertFalse(sat.should_ask("ORDER_STATUS", True, True, True, False))
        self.assertFalse(sat.should_ask("ORDER_STATUS", True, True, False, True))

    def test_an_answer_is_read_only_against_a_pending_ask(self):
        sat, cg = _sat()
        asked = {"satisfaction": {"state": "ASKED", "intent": "ORDER_STATUS"}}
        conf = cg.acr.is_confirmation
        self.assertEqual(sat.answer_of(asked, "yes", conf), "SATISFIED")
        self.assertEqual(sat.answer_of(asked, "haan ji", conf), "SATISFIED")
        self.assertEqual(sat.answer_of(asked, "no", conf), "NOT_SATISFIED")
        self.assertEqual(sat.answer_of(asked, "nahi", conf), "NOT_SATISFIED")
        self.assertEqual(sat.answer_of(asked, "not really", conf), "NOT_SATISFIED")
        self.assertIsNone(sat.answer_of(asked, "where is my refund?", conf))
        self.assertIsNone(sat.answer_of({}, "yes", conf))
        self.assertIsNone(sat.answer_of({"satisfaction": {"state": "RECOVERY"}}, "no", conf))

    def test_the_asks_carry_no_ticket_or_navigation_wording(self):
        sat, cg = _sat()
        for text in sat.ASK.values():
            self.assertIsNone(cg._pending_ask([{"role": "assistant", "content": text}]))
            self.assertFalse(cg.acr.offers_navigation(text))
        self.assertEqual(cg._pending_ask([{"role": "assistant", "content": sat.TICKET_OFFER}]),
                         "create_ticket")


def _session(events=(), traces=(), orders=(), ket=""):
    return {"session_id": "s", "customer_id": "688", "ket_ref": ket, "status": "active",
            "messages": [], "events": [{
                "type": r[1], "at": r[2], "action_id": r[3], "action_type": r[4],
                "success": r[5], "failure": r[6], "stage": r[7], "provider": r[8],
                "model": r[9], "ms": r[10], "payload": acs._json(acs._text(r[11]))}
                for r in events],
            "actions": [], "orders": list(orders), "_traces": list(traces)}


class Report(unittest.TestCase):

    def test_each_state_and_why_it_was_not_asked(self):
        t0 = "2026-10-05 10:00:00"
        s = _session([_ev("s", "SATISFACTION_ASKED", t0, {"turn_intent": "ORDER_STATUS"}),
                      _ev("s", "SATISFACTION_RECORDED", "2026-10-05 10:01:00",
                          {"state": "SATISFIED", "turn_intent": "ORDER_STATUS"})])
        self.assertEqual(acs.satisfaction(s, []), ("SATISFIED", "ORDER_STATUS"))
        s = _session([_ev("s", "SATISFACTION_ASKED", t0, {"turn_intent": "ORDER_STATUS"})])
        self.assertEqual(acs.satisfaction(s, [])[0], "NO_RESPONSE")
        s = _session([_ev("s", "SATISFACTION_RECORDED", t0,
                          {"state": "NOT_SATISFIED", "turn_intent": "RESHIP_STATUS"}),
                      _ev("s", "ESCALATION_OFFERED", "2026-10-05 10:02:00", {})])
        self.assertEqual(acs.satisfaction(s, []),
                         ("NOT_SATISFIED", "RESHIP_STATUS · one recovery answer · ticket offered"))
        s = _session()
        self.assertEqual(acs.satisfaction(s, [{"intent": "PRODUCT_SEARCH"}]),
                         ("NOT_ASKED", "shopping journey still active"))
        self.assertEqual(acs.satisfaction(_session(), []),
                         ("NOT_ASKED", "no terminal support answer"))
        self.assertEqual(acs.satisfaction(_session(orders=[{"order_id": "X"}]), [])[1],
                         "purchase completed")

    def test_the_journey_reads_need_action_page_cart_purchase_ticket_satisfaction(self):
        s = _session([_ev("s", "RECOMMENDATION_GENERATED", "2026-10-05 10:00:00",
                          {"result_count": 7}),
                      _ev("s", "JOURNEY_STAGE", "2026-10-05 10:00:09", stage="PRODUCT")])
        s["actions"] = [{"type": "NAVIGATE", "status": "EXECUTED"}]
        rows = dict(acs.journey(s, [{"intent": "PRODUCT_SEARCH", "tools": []}]))
        self.assertEqual(rows["Need"], "PRODUCT_SEARCH")
        self.assertEqual(rows["Recommendation"], "7 frames")
        self.assertEqual(rows["AI action"], "NAVIGATE EXECUTED")
        self.assertEqual(rows["Reached"], "PRODUCT")
        self.assertEqual((rows["Product viewed"], rows["Cart"], rows["Purchase"]),
                         ("YES", "NO", "NO"))
        self.assertEqual(rows["KET escalation"], "NO")
        self.assertEqual(rows["Satisfaction"], "NOT_ASKED — shopping journey still active")
        rows = dict(acs.journey(_session(), [{"intent": "RESHIP_STATUS",
                                              "tools": [{"tool": "LOOKUP_RESHIP_STATUS"}]}]))
        self.assertEqual(rows["Order / return / refund / reship"], "reship answered")


@unittest.skipUnless(AVAILABLE, "no MariaDB test database (see scripts/setup_test_db.sh)")
class OnMariaDB(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Flask
        cls.cg = sys.modules.get("flaskr.chat_gateway") or _load_gateway()
        cls.db = _connect()
        cur = cls.db.cursor()
        cur.execute(CHAT_SESSIONS_DDL)
        cur.execute(CHAT_MESSAGES_DDL)
        cur.execute(CHAT_EVENTS_DDL)
        for col, ddl in (("customer_id", "INT NULL"), ("current_page_url", "TEXT NULL"),
                         ("ket_ticket_uid", "VARCHAR(64) NULL"),
                         ("ket_ticket_ref", "VARCHAR(64) NULL")):
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE "
                        "TABLE_SCHEMA=DATABASE() AND TABLE_NAME='chat_sessions' "
                        "AND COLUMN_NAME=%s", (col,))
            if not cur.fetchone()["n"]:
                cur.execute("ALTER TABLE chat_sessions ADD COLUMN %s %s" % (col, ddl))
        cls.cg.acr.ensure_schema(_connect)
        cls.cg._get_db = staticmethod(_connect)
        cls.app = Flask(__name__)
        cls.app.config.update(TESTING=True, SECRET_KEY="test-secret",
                              ACR_ACTIONS_ENABLED=True)
        cls.app.register_blueprint(cls.cg.bp)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s["user_id"] = 688
        self.sid = "sat_" + uuid.uuid4().hex[:12]
        self.db.cursor().execute(
            "INSERT INTO chat_sessions (session_id, status, contact_name, contact_email, "
            "customer_id, created_at, last_activity) VALUES (%s, 'active', 'Tess', "
            "'qa@example.com', 688, NOW(), NOW())", (self.sid,))
        cg = self.cg
        self.prompts = []
        self.scripted = []

        def fake_model(system_prompt, history, user_message, **kw):
            self.prompts.append(system_prompt)
            return self.scripted.pop(0) if self.scripted else ("Anything else?", None)

        def orders(db, turn_intent, page_url, session_id):
            if turn_intent in cg.ai_language.ORDER_LOOKUP_INTENTS:
                return "\nORDERS ON FILE\n", {"orders": [{"order_id": "OW-T1"}]}
            return "", None

        self._real = (cg._call_deepseek, cg._lens_context, cg._photo_context,
                      cg._emit_model_events, cg._order_context, cg._rx_context)
        cg._call_deepseek = fake_model
        cg._lens_context = lambda *a, **k: (None, None, None, None)
        cg._photo_context = lambda *a, **k: None
        cg._emit_model_events = lambda *a, **k: []
        cg._order_context = orders
        cg._rx_context = lambda *a, **k: ("", None)

    def tearDown(self):
        cg = self.cg
        (cg._call_deepseek, cg._lens_context, cg._photo_context,
         cg._emit_model_events, cg._order_context, cg._rx_context) = self._real
        cur = self.db.cursor()
        for t in ("chat_messages", "chat_events", "ai_events", "ai_actions", "chat_sessions"):
            cur.execute("DELETE FROM %s WHERE session_id=%%s" % t, (self.sid,))

    def _say(self, text):
        r = self.client.post("/api/chat/message", json={
            "session_id": self.sid, "content": text, "page_url": "https://optiwar.com/",
            "client_message_id": uuid.uuid4().hex})
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def _events(self, etype):
        cur = self.db.cursor()
        cur.execute("SELECT payload FROM ai_events WHERE session_id=%s AND event_type=%s "
                    "ORDER BY created_at", (self.sid, etype))
        return [json.loads(r["payload"] or "{}") for r in cur.fetchall()]

    def test_an_order_answer_asks_once_and_a_yes_is_recorded_without_the_model(self):
        sat = self.cg.satisfaction
        self.scripted = [("Your order OW-T1 was shipped yesterday.", None),
                         ("It is with the courier.", None)]
        out = self._say("where is my order?")
        self.assertTrue(out["reply"].endswith(sat.ASK["en"]), out["reply"])
        self.assertEqual(self._events("SATISFACTION_ASKED"), [{"turn_intent": "ORDER_STATUS"}])
        calls = len(self.prompts)
        out = self._say("yes")
        self.assertEqual(out["reply"], sat.THANKS["en"])
        self.assertEqual(len(self.prompts), calls)
        self.assertNotIn("navigate_url", out)
        self.assertEqual(self._events("SATISFACTION_RECORDED"),
                         [{"state": "SATISFIED", "turn_intent": "ORDER_STATUS"}])
        out = self._say("where is my order now?")
        self.assertNotIn(sat.ASK["en"], out["reply"])
        self.assertEqual(len(self._events("SATISFACTION_ASKED")), 1)

    def test_a_no_gets_one_recovery_that_offers_a_ticket(self):
        sat = self.cg.satisfaction
        self.scripted = [("Your order OW-T1 was shipped yesterday.", None),
                         ("The record shows OW-T1 shipped; no delivery scan yet.", None)]
        self._say("where is my order?")
        out = self._say("no")
        self.assertIn(sat.RECOVERY_SECTION, self.prompts[-1])
        self.assertIn("ORDERS ON FILE", self.prompts[-1])
        self.assertTrue(out["reply"].endswith(sat.TICKET_OFFER), out["reply"])
        self.assertEqual(self._events("SATISFACTION_RECORDED"),
                         [{"state": "NOT_SATISFIED", "turn_intent": "ORDER_STATUS"}])
        self.assertEqual(len(self._events("ESCALATION_OFFERED")), 1)
        self.assertEqual(len(self._events("SATISFACTION_ASKED")), 1)

    def test_a_shopping_reply_is_not_asked(self):
        self.scripted = [("We have round frames in black and tortoise.", None)]
        out = self._say("show me round frames")
        self.assertNotIn(self.cg.satisfaction.ASK["en"], out["reply"])
        self.assertEqual(self._events("SATISFACTION_ASKED"), [])

    def test_a_signed_out_customer_is_not_asked(self):
        with self.client.session_transaction() as s:
            s.pop("user_id", None)
        self.scripted = [("Please sign in to see your orders.", None)]
        out = self._say("where is my order?")
        self.assertNotIn(self.cg.satisfaction.ASK["en"], out["reply"])
