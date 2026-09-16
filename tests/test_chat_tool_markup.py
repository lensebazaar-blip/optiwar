"""A model's unexecuted tool markup is a failed turn, and a "yes" to the
question the assistant just asked is answered by the server.

Reproduced live (session 4312c311505c4bfe): the assistant asked "Would you
like me to create a support ticket for this? Yes or No.", the customer said
"yes", and the next reply was DeepSeek's internal function-call syntax
(``<｜｜DSML｜｜invoke name="search_products">``) shown verbatim. Two faults:
the markup reached the customer, and the confirmed action was not the one
performed.

Unit tests need no database. ``OnMariaDB`` drives ``/api/chat/message`` with
a scripted model against the CI MariaDB and is skipped without one.
"""
import os
import sys
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.test_chat_attachments import (  # noqa: E402
    AVAILABLE, CHAT_EVENTS_DDL, CHAT_MESSAGES_DDL, CHAT_SESSIONS_DDL,
    _connect, _load_gateway)

LEAK_FULLWIDTH = ('<｜｜DSML｜｜ calls> <｜｜DSML｜｜ invoke name="search_products"> '
                  '<｜｜DSML｜｜ parameter name="color" string="true">red</｜｜DSML｜｜ parameter> '
                  '</｜｜DSML｜｜ invoke> </｜｜DSML｜｜ calls>')
LEAK_ASCII_PREFIXED = ('I couldn\'t find BP86, Sachin. Let me search more broadly.\n\n'
                       '<|DSML|tool_calls><|DSML|invoke name="search_products">'
                       '<|DSML|parameter name="keyword" string="true">BP</|DSML|parameter>')
TICKET_ASK = ("Sudhanshu, we don't offer phone support — Optiwar handles all support through "
              "the ticketing system, with a response within 24 hours.\n\n"
              "Would you like me to create a support ticket for this? Yes or No.")
SUPERVISOR_ASK = ("I can't process that order here, so I'd like to pass it to my supervisor. "
                  "Do you want me to connect you with my supervisor? Yes or No")


class Unit(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.cg = sys.modules.get("flaskr.chat_gateway") or _load_gateway()

    def test_markup_is_recognised_in_every_spelling_and_plain_text_is_not(self):
        has = self.cg._has_tool_markup
        self.assertTrue(has(LEAK_FULLWIDTH))
        self.assertTrue(has(LEAK_ASCII_PREFIXED))
        self.assertTrue(has('</｜DSML｜invoke>'))
        self.assertTrue(has('<|| dsml || calls>'))
        self.assertFalse(has("Here are our red frames: BB29, BB90. [ACTION:NAVIGATE:/frames?color=red]"))
        self.assertFalse(has("Your ticket OPTIWA-1025 has been created."))
        self.assertFalse(has(""))
        self.assertFalse(has(None))

    def test_the_question_last_asked_decides_what_a_yes_means(self):
        ask = self.cg._pending_ask
        self.assertEqual(ask([{"role": "assistant", "content": TICKET_ASK}]), "create_ticket")
        self.assertEqual(ask([{"role": "assistant", "content": SUPERVISOR_ASK}]), "human_handover")
        # the customer's own words in between do not change the question
        self.assertEqual(ask([{"role": "assistant", "content": TICKET_ASK},
                              {"role": "user", "content": "hmm"}]), "create_ticket")
        # an older question is not live once the assistant has moved on
        self.assertIsNone(ask([{"role": "assistant", "content": TICKET_ASK},
                               {"role": "user", "content": "no"},
                               {"role": "assistant", "content": "No problem. Anything else?"}]))
        # a statement that mentions a ticket is not a question
        self.assertIsNone(ask([{"role": "assistant",
                                "content": "Your support ticket OPTIWA-1025 has been created."}]))
        self.assertIsNone(ask([]))

    def test_a_yes_to_the_ticket_question_carries_the_ticket_tag_and_nothing_else_does(self):
        reply = self.cg._confirmed_ask_reply
        hist = [{"role": "assistant", "content": TICKET_ASK}]
        r = reply(hist, "yes", "Sudhanshu")
        self.assertIn("[ACTION:CREATE_TICKET]", r)
        self.assertTrue(r.startswith("Sudhanshu, "))
        self.assertNotIn("[ACTION:HUMAN_HANDOVER]", r)
        r = reply([{"role": "assistant", "content": SUPERVISOR_ASK}], "ok", "Visitor")
        self.assertIn("[ACTION:HUMAN_HANDOVER]", r)
        self.assertFalse(r.startswith("Visitor"))
        # not a confirmation, or nothing was asked: the model answers
        self.assertIsNone(reply(hist, "no thanks", "Sudhanshu"))
        self.assertIsNone(reply(hist, "yes but only for the red one", "Sudhanshu"))
        self.assertIsNone(reply([{"role": "assistant", "content": "Anything else?"}], "yes", "S"))

    def test_a_stored_leak_is_left_out_of_the_model_s_history(self):
        rows = [{"role": "assistant", "content": "Hi"},
                {"role": "user", "content": "yes"},
                {"role": "assistant", "content": LEAK_FULLWIDTH},
                {"role": "user", "content": "hello?"}]

        class Cur:
            def execute(self, *a):
                pass

            def fetchall(self):
                return list(reversed(rows))

        class Db:
            def cursor(self):
                return Cur()

        hist = self.cg._get_conversation_history(Db(), "s")
        self.assertEqual([m["content"] for m in hist], ["Hi", "yes", "hello?"])


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
        cls.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        cls.app.register_blueprint(cls.cg.bp)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        self.sid = "tm_" + uuid.uuid4().hex[:12]
        self.db.cursor().execute(
            "INSERT INTO chat_sessions (session_id, status, contact_name, contact_email, "
            "created_at, last_activity) VALUES (%s, 'active', 'Sudhanshu', 's@example.com', NOW(), NOW())",
            (self.sid,))
        cg = self.cg
        self.model_calls = 0
        self.scripted = []

        def fake_model(system_prompt, history, user_message, **kw):
            self.model_calls += 1
            return self.scripted.pop(0) if self.scripted else ("Anything else?", None)

        self.tickets = []

        def fake_ticket(db, session_id, session, page_url, reply, ai_msg_id, actions):
            self.tickets.append(list(actions))
            return reply + " Your support ticket OPTIWA-9 has been created."

        self._real = (cg._call_deepseek, cg._ticket_for_actions, cg._lens_context,
                      cg._photo_context, cg._emit_model_events)
        cg._call_deepseek = fake_model
        cg._ticket_for_actions = fake_ticket
        cg._lens_context = lambda *a, **k: (None, None, None, None)
        cg._photo_context = lambda *a, **k: None
        cg._emit_model_events = lambda *a, **k: None

    def tearDown(self):
        cg = self.cg
        (cg._call_deepseek, cg._ticket_for_actions, cg._lens_context,
         cg._photo_context, cg._emit_model_events) = self._real
        cur = self.db.cursor()
        for t in ("chat_messages", "chat_events", "chat_sessions"):
            cur.execute("DELETE FROM %s WHERE session_id=%%s" % t, (self.sid,))

    def _say(self, text):
        return self.client.post("/api/chat/message", json={
            "session_id": self.sid, "content": text, "page_url": "https://optiwar.in/x",
            "client_message_id": uuid.uuid4().hex})

    def _assistant_rows(self):
        cur = self.db.cursor()
        cur.execute("SELECT content, status FROM chat_messages WHERE session_id=%s AND role='assistant' "
                    "ORDER BY id", (self.sid,))
        return cur.fetchall()

    def test_yes_to_the_ticket_question_creates_the_ticket_without_asking_the_model(self):
        self.scripted = [(TICKET_ASK, None)]
        self.assertEqual(self._say("ask your agent to call me").status_code, 200)
        r = self._say("yes")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(self.model_calls, 1)                 # only the first turn
        self.assertEqual(self.tickets, [["create_ticket"]])
        self.assertIn("OPTIWA-9", body["reply"])
        self.assertNotIn("[ACTION:", body["reply"])
        self.assertNotIn("DSML", body["reply"])

    def test_a_leak_is_retried_once_and_a_second_leak_fails_the_turn_generically(self):
        self.scripted = [(LEAK_FULLWIDTH, None), ("Here are our red frames.", None)]
        body = self._say("find red frames").get_json()
        self.assertEqual(self.model_calls, 2)
        self.assertEqual(body["reply"], "Here are our red frames.")

        self.scripted = [(LEAK_FULLWIDTH, None), (LEAK_ASCII_PREFIXED, None)]
        body = self._say("and blue ones").get_json()
        self.assertEqual(self.model_calls, 4)
        self.assertEqual(body["status"], "failed")
        self.assertNotIn("DSML", body["reply"])
        for row in self._assistant_rows():
            self.assertNotIn("DSML", row["content"])
        cur = self.db.cursor()
        cur.execute("SELECT event_type FROM chat_events WHERE session_id=%s AND event_type IN "
                    "('ai_tool_markup_leak','ai_failed') ORDER BY id", (self.sid,))
        self.assertEqual([r["event_type"] for r in cur.fetchall()],
                         ["ai_tool_markup_leak"] * 3 + ["ai_failed"])


if __name__ == "__main__":
    unittest.main()
