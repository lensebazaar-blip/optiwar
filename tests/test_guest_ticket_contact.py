"""Escalation tickets need reachable customer contact information."""
import json
import os
import sys
import unittest
import uuid
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.test_chat_attachments import (  # noqa: E402
    AVAILABLE, CHAT_EVENTS_DDL, CHAT_MESSAGES_DDL, CHAT_SESSIONS_DDL,
    _connect, _load_gateway)


TICKET_ASK = "Would you like me to create a support ticket for this? Yes or No."
HUMAN_ASK = ("I can't process that order here, so I'd like to pass it to my supervisor. "
             "Do you want me to connect you with my supervisor? Yes or No")


@unittest.skipUnless(AVAILABLE, "no MariaDB test database (see scripts/setup_test_db.sh)")
class GuestTicketContact(unittest.TestCase):

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
        cls.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        cls.app.register_blueprint(cls.cg.bp)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        self.client = self.app.test_client()
        self.sids = []
        self.prompts = []
        self.scripted = []
        self.tickets = []
        self.ticket_dispatches = []
        cg = self.cg

        def fake_model(system_prompt, history, user_message, **kwargs):
            self.prompts.append(system_prompt)
            return self.scripted.pop(0) if self.scripted else ("Anything else?", None)

        def fake_forward(db, session_id, session, page_url, phone="", classification=None):
            self.tickets.append({
                "email": session.get("contact_email"),
                "phone": phone,
                "classification": classification,
            })
            return 77, None

        ticket_for_actions = cg._ticket_for_actions

        def track_ticket_dispatch(*args, **kwargs):
            self.ticket_dispatches.append((args, kwargs))
            return ticket_for_actions(*args, **kwargs)

        names = ("_call_deepseek", "_forward_ticket_from_chat", "_send_ticket_email",
                 "_send_ticket_whatsapp", "_send_ticket_sms", "_generate_chat_summary",
                 "_lens_context", "_photo_context", "_emit_model_events",
                 "_ticket_for_actions")
        self._real = {name: getattr(cg, name) for name in names}
        cg._call_deepseek = fake_model
        cg._forward_ticket_from_chat = fake_forward
        cg._send_ticket_email = lambda *args, **kwargs: None
        cg._send_ticket_whatsapp = lambda *args, **kwargs: None
        cg._send_ticket_sms = lambda *args, **kwargs: None
        cg._generate_chat_summary = lambda *args, **kwargs: ""
        cg._lens_context = lambda *args, **kwargs: (None, None, None, None)
        cg._photo_context = lambda *args, **kwargs: None
        cg._emit_model_events = lambda *args, **kwargs: None
        cg._ticket_for_actions = track_ticket_dispatch

    def tearDown(self):
        for name, function in self._real.items():
            setattr(self.cg, name, function)
        cur = self.db.cursor()
        for sid in self.sids:
            for table in ("chat_messages", "chat_events", "chat_sessions", "ai_events"):
                cur.execute("DELETE FROM %s WHERE session_id=%%s" % table, (sid,))

    def _sign_in(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 424242
            session["user_email"] = "canary@example.com"
            session["user_name"] = "Canary"
            session["user_phone"] = "9810012345"

    def _start(self):
        response = self.client.post("/api/chat/start", json={
            "page_url": "https://optiwar.in/", "entry": "contact_us"})
        self.assertEqual(response.status_code, 200)
        session_id = response.get_json()["session_id"]
        self.sids.append(session_id)
        return session_id

    def _say(self, session_id, content):
        return self.client.post("/api/chat/message", json={
            "session_id": session_id,
            "content": content,
            "page_url": "https://optiwar.in/",
            "client_message_id": uuid.uuid4().hex,
        })

    def _latest_assistant(self, session_id):
        cur = self.db.cursor()
        cur.execute("SELECT content, metadata FROM chat_messages WHERE session_id=%s "
                    "AND role='assistant' ORDER BY id DESC LIMIT 1", (session_id,))
        return cur.fetchone()

    def _events(self, session_id):
        cur = self.db.cursor()
        cur.execute("SELECT event_type, payload FROM ai_events WHERE session_id=%s "
                    "ORDER BY created_at", (session_id,))
        return cur.fetchall()

    def _offer_ticket(self, session_id, offer=TICKET_ASK):
        self.scripted.append((offer, None))
        response = self._say(session_id, "I have a question about my prism lenses.")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIn("Would you like", response.get_json()["reply"])

    def test_guest_confirmation_of_ticket_offer_asks_for_contact_without_filing(self):
        session_id = self._start()
        self._offer_ticket(session_id)
        response = self._say(session_id, "Yes")
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        expected, _written = self.cg.ai_language.support_reply("ask_contact_human", "en")
        self.assertEqual(body["reply"], expected)
        self.assertNotIn("[ACTION:", body["reply"])
        self.assertFalse(body.get("actions"))
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.ticket_dispatches, [])
        self.assertEqual(self.tickets, [])

        assistant = self._latest_assistant(session_id)
        metadata = json.loads(assistant["metadata"])
        self.assertEqual(metadata["await_contact"],
                         self.cg.ai_language.INTENT_HUMAN_REQUEST)
        self.assertEqual(metadata["phone"], "")
        self.assertEqual(metadata["email"], "")

        events = self._events(session_id)
        contact_offers = [
            event for event in events
            if event["event_type"] == self.cg.acr.EV_ESCALATION_OFFERED
            and json.loads(event["payload"]).get("offer") == "ask_contact_human"
        ]
        self.assertEqual(len(contact_offers), 1)
        self.assertNotIn(self.cg.acr.EV_TICKET_CREATED,
                         [event["event_type"] for event in events])

    def test_guest_can_provide_email_on_the_next_turn_and_file_the_ticket(self):
        session_id = self._start()
        self._offer_ticket(session_id)
        self._say(session_id, "Yes")

        response = self._say(session_id, "Please use guest@example.com")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(len(self.tickets), 1)
        self.assertEqual(self.tickets[0]["email"], "guest@example.com")
        self.assertEqual(self.tickets[0]["classification"]["ticket_reason"],
                         self.cg.ai_language.INTENT_HUMAN_REQUEST)
        self.assertIn("#77", response.get_json()["reply"])
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(len(self.ticket_dispatches), 1)

    def test_signed_in_confirmation_still_files_the_ticket(self):
        self._sign_in()
        session_id = self._start()
        self._offer_ticket(session_id)
        response = self._say(session_id, "Yes")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(len(self.tickets), 1)
        self.assertEqual(self.tickets[0]["email"], "canary@example.com")
        self.assertIn("#77", response.get_json()["reply"])
        self.assertEqual(len(self.ticket_dispatches), 1)

    def test_guest_human_handover_confirmation_also_asks_for_contact(self):
        session_id = self._start()
        self.scripted.append((HUMAN_ASK, None))
        self._say(session_id, "I have a question about my prism lenses.")
        response = self._say(session_id, "Yes")
        self.assertEqual(response.status_code, 200, response.get_json())
        expected, _written = self.cg.ai_language.support_reply("ask_contact_human", "en")
        self.assertEqual(response.get_json()["reply"], expected)
        self.assertEqual(self.tickets, [])
        self.assertEqual(self.ticket_dispatches, [])


class TicketEmail(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.cg = sys.modules.get("flaskr.chat_gateway") or _load_gateway()

    def test_missing_customer_email_skips_only_customer_message(self):
        from flask import Flask
        app = Flask(__name__)
        sent = mock.MagicMock()
        sent.__enter__.return_value = sent
        with app.app_context(), mock.patch.dict(os.environ, {
                    "SMTP_HOST": "localhost", "SMTP_PORT": "1025",
                    "SMTP_USERNAME": "user", "SMTP_PASSWORD": "pass"}), \
                mock.patch.object(self.cg.smtplib, "SMTP", return_value=sent) as smtp, \
                self.assertLogs(app.logger, level="INFO") as captured:
            result = self.cg._send_ticket_email("", "Guest", 123, "Prism issue", "/chat")

        self.assertTrue(result)
        smtp.assert_called_once()
        sent.send_message.assert_called_once()
        admin_message = sent.send_message.call_args[0][0]
        self.assertEqual(admin_message["To"], self.cg.ADMIN_EMAIL)
        self.assertFalse(any("ERROR" in line for line in captured.output))
