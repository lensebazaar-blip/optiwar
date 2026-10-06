"""A confirmation of the assistant's live NAVIGATE offer opens exactly what
was offered, for every customer, without asking the model again.

Production showed the fault: a customer's "yes" went back to the model, which
searched again, offered a new link and let the first offer expire. ``OnMariaDB``
drives ``/api/chat/message`` with a scripted model against the CI MariaDB and is
skipped without one.
"""
import json
import os
import sys
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.test_chat_attachments import (  # noqa: E402
    AVAILABLE, CHAT_EVENTS_DDL, CHAT_MESSAGES_DDL, CHAT_SESSIONS_DDL,
    _connect, _load_gateway)

OFFER = "Here are 7 aviator frames. Would you like me to take you to these frames?"
TARGET = "/frames?shape=aviator"


class Unit(unittest.TestCase):

    def test_a_reply_asks_before_navigating_only_with_a_navigation_question(self):
        import acr
        asks = acr.asks_before_navigating
        self.assertTrue(asks("Here are 5 frames. Would you like me to take you there?"))
        self.assertTrue(asks("Would you like me to show you these? "
                             "Click here to let me take you there"))
        self.assertTrue(asks("Shall I show you these? You can click here to let me "
                             "take you there."))
        self.assertFalse(asks("Here are the frames. Let me know if you want another colour."))
        self.assertFalse(asks("Taking you there now. Would you like me to show you more?"))
        self.assertFalse(asks("Opening them now, let me know if you want another colour."))
        self.assertFalse(asks("Shall I raise a ticket for you?"))

    def test_a_reply_names_the_one_page_its_own_buttons_open(self):
        import acr
        link = acr.reply_link_target
        self.assertEqual(link("Would you like me to take you there?\n\n"
                              "[\u25b6 Open My Orders](/profile/?tab=orders)"), "/profile/?tab=orders")
        self.assertEqual(link("[a](/x) and again [b](/x)"), "/x")
        self.assertIsNone(link("Would you like me to take you there?"))
        self.assertIsNone(link("[a](/x) or [b](/y)"))
        self.assertIsNone(link("[a](https://evil.example/x)"))
        self.assertIsNone(link("[a](//evil.example/x)"))

    def test_the_widget_stashes_the_offer_its_button_opens(self):
        with open(os.path.join(REPO, "static", "js", "chat-widget.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertNotIn("lastOffer", js)
        self.assertIn("stashActionForArrival({ action_id: id }, a.getAttribute('href'));", js)
        self.assertIn("stashActionForArrival(acrAction, data.navigate_url);", js)
        self.assertIn("var arrived = !!(d && d.target && pathOf(d.target) === here);", js)
        self.assertIn("if (!arrived || !d.session_id || !d.action_id) return;", js)
        self.assertIn("var id = a && a.getAttribute('data-ow-action-id');", js)
        self.assertIn("renderMsgDirect('ai', data.reply, false, replyTime, data.offer);", js)
        self.assertIn("renderMsgDirect(type, m.content, true, m.created_at, m.offer);", js)
        self.assertIn("old[i].removeAttribute('data-ow-action-id');", js)

    def test_the_returned_model_is_kept_apart_from_the_requested_one(self):
        cg = sys.modules.get("flaskr.chat_gateway") or _load_gateway()
        out = cg._model_call_trace([
            {"kind": "model_call", "provider": "deepseek", "model": "deepseek-v4-flash",
             "actual_model": "deepseek-flash", "success": True},
            {"kind": "model_call", "provider": "deepseek", "model": "deepseek-v4-flash",
             "success": False, "failure_code": "http_400"}])
        self.assertEqual(out[0]["model"], "deepseek-v4-flash")
        self.assertEqual(out[0]["returned_model"], "deepseek-flash")
        self.assertNotIn("returned_model", out[1])


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
                         ("ket_ticket_uid", "VARCHAR(64) NULL"), ("ket_ticket_ref", "VARCHAR(64) NULL")):
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                        "AND TABLE_NAME='chat_sessions' AND COLUMN_NAME=%s", (col,))
            if not cur.fetchone()["n"]:
                cur.execute("ALTER TABLE chat_sessions ADD COLUMN %s %s" % (col, ddl))
        cls.cg.acr.ensure_schema(_connect)
        cls.cg._get_db = staticmethod(_connect)
        cls.app = Flask(__name__)
        # Canary-only with no canary cookie or allow-listed email: an ordinary
        # shopper. NAVIGATE must still be a recorded action for them.
        cls.app.config.update(TESTING=True, SECRET_KEY="test-secret",
                              ACR_ACTIONS_ENABLED=True, ACR_CANARY_ONLY=True,
                              ACR_CANARY_EMAILS="staff@example.com")
        cls.app.register_blueprint(cls.cg.bp)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        self.app.config["ACR_ACTIONS_ENABLED"] = True
        self.sid = "nc_" + uuid.uuid4().hex[:12]
        self.db.cursor().execute(
            "INSERT INTO chat_sessions (session_id, status, contact_name, contact_email, "
            "created_at, last_activity) VALUES (%s, 'active', 'Girish', 'shopper@example.com', NOW(), NOW())",
            (self.sid,))
        cg = self.cg
        self.model_calls = 0
        self.scripted = []

        def fake_model(system_prompt, history, user_message, **kw):
            self.model_calls += 1
            return self.scripted.pop(0) if self.scripted else ("Anything else?", None)

        self._real = (cg._call_deepseek, cg._lens_context, cg._photo_context,
                      cg._emit_model_events, cg._recover_nav_target)
        cg._call_deepseek = fake_model
        cg._lens_context = lambda *a, **k: (None, None, None, None)
        cg._photo_context = lambda *a, **k: None
        cg._emit_model_events = lambda *a, **k: []
        cg._recover_nav_target = lambda: TARGET

    def tearDown(self):
        cg = self.cg
        (cg._call_deepseek, cg._lens_context, cg._photo_context,
         cg._emit_model_events, cg._recover_nav_target) = self._real
        cur = self.db.cursor()
        for t in ("chat_messages", "chat_events", "ai_events", "ai_actions", "chat_sessions"):
            cur.execute("DELETE FROM %s WHERE session_id=%%s" % t, (self.sid,))

    def _say(self, text):
        r = self.client.post("/api/chat/message", json={
            "session_id": self.sid, "content": text, "page_url": "https://optiwar.com/",
            "client_message_id": uuid.uuid4().hex})
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def _actions(self):
        cur = self.db.cursor()
        cur.execute("SELECT action_id, target, status FROM ai_actions WHERE session_id=%s "
                    "ORDER BY created_at, action_id", (self.sid,))
        return cur.fetchall()

    def _last_trace(self):
        cur = self.db.cursor()
        cur.execute("SELECT metadata FROM chat_messages WHERE session_id=%s AND role='assistant' "
                    "ORDER BY id DESC LIMIT 1", (self.sid,))
        return json.loads(cur.fetchone()["metadata"])["trace"]

    def _offer(self):
        self.scripted = [(OFFER, None)]
        body = self._say("show me aviators")
        self.assertNotIn("navigate_url", body)
        offered = self._actions()
        self.assertEqual([(a["target"], a["status"]) for a in offered], [(TARGET, "PENDING")])
        self.assertEqual(self._last_trace()["action"],
                         {"id": offered[0]["action_id"], "type": "NAVIGATE",
                          "target": TARGET, "state": "OFFERED"})
        return offered[0]["action_id"]

    def test_every_confirmation_word_opens_the_offer_without_the_model(self):
        for word in ("yes", "yeah", "yes please", "open it", "haan", "ha", "ji",
                     "theek hai", "chalo", "take me there", "show me"):
            with self.subTest(word=word):
                self.tearDown()
                self.setUp()
                offered = self._offer()
                body = self._say(word)
                self.assertEqual(self.model_calls, 1, word)
                self.assertEqual(body["navigate_url"], TARGET)
                self.assertEqual(body["action"]["action_id"], offered)
                self.assertTrue(body["reply"].startswith(
                    tuple(self.cg.NAV_CONFIRM_REPLY.values())))
                acts = self._actions()
                self.assertEqual([(a["action_id"], a["status"]) for a in acts],
                                 [(offered, "CONFIRMED")])
                t = self._last_trace()
                self.assertEqual((t["source"], t["trigger"], t["model_calls"]),
                                 ("DETERMINISTIC", "action_confirmation", []))
                self.assertEqual(t["action"], {"id": offered, "type": "NAVIGATE",
                                               "target": TARGET, "state": "CONFIRMED",
                                               "bound_to_offer": True})

    def test_the_browser_arriving_marks_the_same_action_executed(self):
        offered = self._offer()
        self._say("yes")
        ok = self.cg.acr.record_action_result(self.db, offered, True)
        self.assertTrue(ok)
        self.assertEqual([a["status"] for a in self._actions()], ["EXECUTED"])

    def test_without_a_live_offer_a_yes_goes_to_the_model(self):
        self._say("yes")
        self.assertEqual(self.model_calls, 1)
        self.assertEqual(self._actions(), ())

    def test_an_offer_the_assistant_moved_on_from_is_not_opened(self):
        self._offer()
        self.scripted = [("We ship worldwide in 7-10 days.", None)]
        self._say("how long is shipping")
        self._say("yes")
        self.assertEqual(self.model_calls, 3)
        self.assertNotEqual(self._last_trace()["source"], "DETERMINISTIC")

    def test_an_expired_offer_is_not_opened(self):
        offered = self._offer()
        self.db.cursor().execute("UPDATE ai_actions SET expires_at=NOW() - INTERVAL 1 MINUTE "
                                 "WHERE action_id=%s", (offered,))
        self._say("yes")
        self.assertEqual(self.model_calls, 2)

    def test_a_model_navigation_is_a_recorded_action_for_an_ordinary_shopper(self):
        self.scripted = [("Here they are. [ACTION:NAVIGATE:/frames?shape=round]", None)]
        body = self._say("round frames please")
        acts = self._actions()
        self.assertEqual([(a["target"], a["status"]) for a in acts],
                         [("/frames?shape=round", "CONFIRMED")])
        self.assertEqual(body["action"]["action_id"], acts[0]["action_id"])
        self.assertNotEqual(self._last_trace()["action"]["state"], "AUTO_NAVIGATE")

    def test_a_reply_that_asks_and_links_is_an_offer_the_yes_opens(self):
        self.scripted = [(OFFER + "\n\n[ACTION:NAVIGATE:/frames?shape=round]", None)]
        body = self._say("round frames please")
        self.assertNotIn("navigate_url", body)
        self.assertNotIn("navigate", body["actions"])
        self.assertIn("](/frames?shape=round)", body["reply"])
        acts = self._actions()
        self.assertEqual([(a["target"], a["status"]) for a in acts],
                         [("/frames?shape=round", "PENDING")])
        self.assertEqual(self._last_trace()["action"]["state"], "OFFERED")
        body = self._say("yes")
        self.assertEqual(self.model_calls, 1)
        self.assertEqual(body["navigate_url"], "/frames?shape=round")
        self.assertEqual(body["action"]["action_id"], acts[0]["action_id"])
        self.assertEqual([a["status"] for a in self._actions()], ["CONFIRMED"])

    def test_an_offer_whose_only_destination_is_its_own_link_is_opened_by_the_yes(self):
        self.cg._recover_nav_target = lambda: None
        self.scripted = [("Your order is confirmed. Full details are in My Orders. Would you "
                          "like me to take you there?\n\n[\u25b6 Open My Orders](/profile/?tab=orders)",
                          None)]
        body = self._say("hmm ok")
        self.assertNotIn("navigate_url", body)
        acts = self._actions()
        self.assertEqual([(a["target"], a["status"]) for a in acts],
                         [("/profile/?tab=orders", "PENDING")])
        self.assertEqual(self._last_trace()["action"]["state"], "OFFERED")
        body = self._say("yes")
        self.assertEqual(self.model_calls, 1)
        self.assertEqual(body["navigate_url"], "/profile/?tab=orders")
        self.assertEqual(body["action"]["action_id"], acts[0]["action_id"])
        self.assertEqual(self._last_trace()["source"], "DETERMINISTIC")

    def test_the_prompts_own_offer_wording_is_an_offer(self):
        self.scripted = [("Would you like me to show you these? Click here to let me take "
                          "you there [ACTION:NAVIGATE:/frames?shape=round]", None)]
        body = self._say("round frames please")
        self.assertNotIn("navigate_url", body)
        acts = self._actions()
        self.assertEqual(body["offer"], {"action_id": acts[0]["action_id"],
                                         "target": "/frames?shape=round"})
        self.assertEqual([a["status"] for a in acts], ["PENDING"])

    def test_an_offer_opened_from_its_button_is_confirmed_then_executed(self):
        offered = self._offer()
        self.assertTrue(self.cg.acr.record_action_result(self.db, offered, True))
        self.assertEqual([a["status"] for a in self._actions()], ["EXECUTED"])
        cur = self.db.cursor()
        cur.execute("SELECT event_type FROM ai_events WHERE session_id=%s AND action_id=%s",
                    (self.sid, offered))
        self.assertEqual(sorted(r["event_type"] for r in cur.fetchall()),
                         ["ACTION_CONFIRMED", "ACTION_EXECUTED", "NAVIGATION_OFFERED"])

    def _history(self):
        real = self.cg._is_chat_owner
        self.cg._is_chat_owner = lambda sid: sid == self.sid
        try:
            r = self.client.get("/api/chat/messages/" + self.sid)
        finally:
            self.cg._is_chat_owner = real
        self.assertEqual(r.status_code, 200)
        return [m for m in r.get_json()["messages"] if m["source"] == "ai"]

    def test_a_restored_chat_carries_the_live_offer_on_its_own_message(self):
        offered = self._offer()
        self.assertEqual([m.get("offer") for m in self._history()],
                         [{"action_id": offered, "target": TARGET}])

    def test_only_the_newest_offer_is_live_after_a_second_one(self):
        first = self._offer()
        self.scripted = [(OFFER, None)]
        self._say("show me aviators again")
        acts = {a["action_id"]: a["status"] for a in self._actions()}
        self.assertEqual(acts[first], "SUPERSEDED")
        second = [k for k, v in acts.items() if v == "PENDING"][0]
        self.assertEqual([m.get("offer") for m in self._history()],
                         [None, {"action_id": second, "target": TARGET}])

    def test_a_superseded_offers_button_executes_nothing(self):
        first = self._offer()
        self.scripted = [(OFFER, None)]
        self._say("show me aviators again")
        self.assertFalse(self.cg.acr.record_action_result(self.db, first, True))
        acts = {a["action_id"]: a["status"] for a in self._actions()}
        self.assertEqual(acts[first], "SUPERSEDED")
        cur = self.db.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM ai_events WHERE action_id=%s "
                    "AND event_type IN ('ACTION_CONFIRMED','ACTION_EXECUTED')", (first,))
        self.assertEqual(cur.fetchone()["n"], 0)

    def test_an_executed_action_is_recorded_once(self):
        offered = self._offer()
        self.assertTrue(self.cg.acr.record_action_result(self.db, offered, True))
        self.assertFalse(self.cg.acr.record_action_result(self.db, offered, True))
        cur = self.db.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM ai_events WHERE action_id=%s "
                    "AND event_type='ACTION_EXECUTED'", (offered,))
        self.assertEqual(cur.fetchone()["n"], 1)

    def test_a_follow_up_question_that_is_not_an_offer_still_navigates(self):
        self.scripted = [("Here are the frames. Let me know if you want another colour. "
                          "[ACTION:NAVIGATE:/frames?shape=round]", None)]
        body = self._say("round frames please")
        self.assertEqual(body["navigate_url"], "/frames?shape=round")
        self.assertNotIn("offer", body)

    def test_a_reply_that_says_it_is_going_there_still_navigates(self):
        self.scripted = [("Opening them now, let me know if you want another colour. "
                          "[ACTION:NAVIGATE:/frames?shape=round]", None)]
        body = self._say("round frames please")
        self.assertEqual(body["navigate_url"], "/frames?shape=round")
        self.assertEqual([a["status"] for a in self._actions()], ["CONFIRMED"])

    def test_with_actions_switched_off_the_legacy_path_is_unchanged(self):
        self.app.config["ACR_ACTIONS_ENABLED"] = False
        self.scripted = [(OFFER, None)]
        self._say("show me aviators")
        self._say("yes")
        self.assertEqual(self.model_calls, 2)
        self.assertEqual(self._actions(), ())


if __name__ == "__main__":
    unittest.main()
