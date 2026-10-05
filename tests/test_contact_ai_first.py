"""Contact Us is the Optiwar assistant, signed in or not.

Every visible support entry opens the one assistant; a ticket is the
assistant's own escalation, never a form the customer fills. A callback or a
person is a server decision recorded apart from what the customer came for
(``original_intent`` / ``final_action`` / ``ticket_reason`` /
``ai_failure_reason``). A signed-out browser owns its anonymous chat through
the signed owner cookie and is sent to sign in for account questions. The
retired ticket-intake routes answer without calling a model or filing.

``OnMariaDB`` drives ``/api/chat`` with a scripted model against the CI
MariaDB and is skipped without one.
"""
import json
import os
import sys
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import ai_language  # noqa: E402
import order_lookup  # noqa: E402
from tests.test_chat_attachments import (  # noqa: E402
    AVAILABLE, CHAT_EVENTS_DDL, CHAT_MESSAGES_DDL, CHAT_SESSIONS_DDL,
    _connect, _load_crm, _load_gateway)


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


class EntryPoints(unittest.TestCase):

    def test_every_support_entry_names_itself_and_opens_the_assistant(self):
        self.assertIn("openContactChoice('order_support')", _read("templates/profile.html"))
        self.assertIn("openContactChoice('order_success')", _read("templates/success.html"))
        self.assertIn("openContactChoice('faq')", _read("templates/ai_faq.html"))
        self.assertNotIn("osp-help-visible')", _read("templates/success.html"))

    def test_no_template_carries_the_manual_ticket_form(self):
        tdir = os.path.join(REPO, "templates")
        for name in os.listdir(tdir):
            if not name.endswith(".html"):
                continue
            html = _read(os.path.join("templates", name))
            for form in ("Select a subject", "Submit Support Request", "/contact_us/submit",
                         "/contact_us/ai_chat", "/contact_us/captcha"):
                self.assertNotIn(form, html, "%s: %s" % (name, form))

    def test_the_widget_loads_for_a_signed_out_page_too(self):
        base = _read("templates/base.html")
        cfg = base[base.index("window.__optiwarChat = {"):]
        self.assertIn("signedIn:", cfg[:200])
        self.assertNotIn("email:", cfg[:200])
        chat = base.index("js/chat-widget.js")
        self.assertLess(base.rindex("{% endif %}", 0, base.index("window.__optiwarChat")),
                        chat)
        js = _read("static/js/chat-widget.js")
        self.assertNotIn("if (!userEmail) return;", js)
        self.assertIn("apiCall('POST', '/start', {", js)
        self.assertNotIn("email: userEmail", js)


class LegacyRoutes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Flask
        cls.crm = _load_crm()
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="t")
        app.register_blueprint(cls.crm.bp)
        cls.client = app.test_client()

    def test_a_bookmark_is_sent_to_the_assistant(self):
        r = self.client.get("/contact_us")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith(self.crm.ASSISTANT_URL))
        self.assertEqual(self.client.post("/contact_us").status_code, 303)

    def test_the_old_bot_calls_no_model_and_files_nothing(self):
        body = self.client.post("/contact_us/ai_chat", json={
            "messages": [{"role": "user", "content": "mujhe callback chahiye"}]}).get_json()
        self.assertIsNone(body["ticket_data"])
        self.assertFalse(body["done"])
        self.assertTrue(body["open_assistant"])
        crm = _read("crm.py")
        bot = crm[crm.index("def ai_chat("):]
        self.assertNotIn("OpenAI", bot[:bot.index("\n@bp.route")])
        self.assertNotIn("gpt-3.5", crm)

    def test_a_legacy_hit_is_committed_with_route_and_method_only(self):
        import types

        class _Cur:
            def __init__(self, rows):
                self.rows = rows

            def execute(self, sql, params):
                self.rows.append(params)

        class _Db:
            def __init__(self):
                self.rows, self.committed = [], 0

            def cursor(self):
                return _Cur(self.rows)

            def commit(self):
                self.committed += 1

        fake = _Db()
        dbmod = types.ModuleType("flaskr_crm_test.db")
        dbmod.get_db = lambda: fake
        saved = sys.modules.get(dbmod.__name__)
        sys.modules[dbmod.__name__] = dbmod
        try:
            self.client.get("/contact_us")
        finally:
            if saved is None:
                sys.modules.pop(dbmod.__name__, None)
            else:
                sys.modules[dbmod.__name__] = saved
        self.assertEqual(fake.committed, 1)
        (row,) = fake.rows
        self.assertEqual(row[1], "LEGACY_SUPPORT_ROUTE")
        self.assertEqual(json.loads(row[10]), {"route": "contact_us", "method": "GET"})

    def test_the_old_forms_are_gone_with_a_pointer(self):
        for method, url in (("get", "/contact_us/captcha"), ("post", "/contact_us/submit"),
                            ("post", "/contact_us/ai_submit")):
            r = getattr(self.client, method)(url, json={})
            self.assertEqual(r.status_code, 410, url)
            self.assertTrue(r.get_json()["open_assistant"], url)

    def test_the_ticket_helpers_stay(self):
        for name in ("_forward_to_ket", "_forward_ticket_from_chat", "persist_ticket_mapping",
                     "ket_attachment_upload"):
            self.assertIn("def %s(" % name, _read("crm.py") + _read("chat_gateway.py"), name)


class CallbackIsAnIntent(unittest.TestCase):

    def test_callback_and_person_are_told_apart(self):
        for m in ("mujhe callback chahiye", "mujhe koi call kar de", "please call me back"):
            self.assertEqual(ai_language.classify_intent(m)[0],
                             ai_language.INTENT_CALLBACK_REQUEST, m)
        self.assertEqual(ai_language.classify_intent("I want a human")[0],
                         ai_language.INTENT_HUMAN_REQUEST)

    def test_a_callback_ticket_is_the_customers_request_not_an_ai_failure(self):
        msgs = ["mujhe koi call kar de"]
        c = ai_language.classify_ticket(msgs, ticket_reason=ai_language.ticket_reason(msgs))
        self.assertEqual(
            {k: c[k] for k in ("original_intent", "final_action", "ticket_reason",
                               "escalation_reason", "ai_failure_reason")},
            {"original_intent": "CALLBACK_REQUEST", "final_action": "CREATE_TICKET",
             "ticket_reason": "CALLBACK_REQUEST",
             "escalation_reason": "CUSTOMER_REQUESTED_HUMAN", "ai_failure_reason": None})

    def test_what_they_came_for_is_kept_apart_from_the_callback(self):
        msgs = ["mera power kya save hai", "mujhe callback chahiye"]
        self.assertEqual(ai_language.ticket_reason(msgs), "CALLBACK_REQUEST")
        c = ai_language.classify_ticket(msgs, ticket_reason="CALLBACK_REQUEST")
        self.assertEqual(c["original_intent"], "PRESCRIPTION_STATUS")
        self.assertEqual(c["escalation_reason"], "CUSTOMER_REQUESTED_HUMAN")

    def test_the_natural_questions(self):
        for m, want in (("mera order kaha hai", "ORDER_STATUS"),
                        ("mera power kya save hai", "PRESCRIPTION_STATUS"),
                        ("payment ho gaya kya", "PAYMENT_STATUS"),
                        ("parcel return ho gaya hai", "RESHIP_STATUS"),
                        ("reship kaise karu", "RESHIP_STATUS"),
                        ("frame fit hoga?", "PRODUCT_SEARCH")):
            self.assertEqual(ai_language.classify_intent(m)[0], want, m)

    def test_the_servers_own_replies_are_in_the_customers_language(self):
        en, _ = ai_language.support_reply("ask_phone", "en")
        hl, lang = ai_language.support_reply("ask_phone", "hi-Latn")
        self.assertEqual(lang, "hi-Latn")
        self.assertNotEqual(en, hl)
        self.assertEqual(ai_language.support_reply("ask_phone", "ta")[1], "en")


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
        cls.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        cls.app.register_blueprint(cls.cg.bp)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        cg = self.cg
        self.client = self.app.test_client()
        self.sids = []
        self.prompts = []
        self.scripted = []
        self.tickets = []

        def fake_model(system_prompt, history, user_message, **kw):
            self.prompts.append(system_prompt)
            return self.scripted.pop(0) if self.scripted else ("Anything else?", None)

        def fake_forward(db, session_id, session, page_url, phone='', classification=None):
            self.tickets.append({"phone": phone, "classification": classification,
                                 "email": session.get("contact_email")})
            return 77, None

        names = ("_call_deepseek", "_forward_ticket_from_chat", "_send_ticket_email",
                 "_send_ticket_whatsapp", "_send_ticket_sms", "_generate_chat_summary",
                 "_lens_context", "_photo_context", "_emit_model_events")
        self._real = {n: getattr(cg, n) for n in names}
        cg._call_deepseek = fake_model
        cg._forward_ticket_from_chat = fake_forward
        cg._send_ticket_email = lambda *a, **k: None
        cg._send_ticket_whatsapp = lambda *a, **k: None
        cg._send_ticket_sms = lambda *a, **k: None
        cg._generate_chat_summary = lambda *a, **k: ""
        cg._lens_context = lambda *a, **k: (None, None, None, None)
        cg._photo_context = lambda *a, **k: None
        cg._emit_model_events = lambda *a, **k: None

    def tearDown(self):
        for n, f in self._real.items():
            setattr(self.cg, n, f)
        cur = self.db.cursor()
        for sid in self.sids:
            for t in ("chat_messages", "chat_events", "chat_sessions", "ai_events"):
                cur.execute("DELETE FROM %s WHERE session_id=%%s" % t, (sid,))

    def _sign_in(self, phone="9810012345"):
        with self.client.session_transaction() as s:
            s["user_id"] = 424242
            s["user_email"] = "canary@example.com"
            s["user_name"] = "Canary"
            s["user_phone"] = phone

    def _start(self, entry="contact_us"):
        r = self.client.post("/api/chat/start", json={
            "page_url": "https://optiwar.in/", "entry": entry})
        self.assertEqual(r.status_code, 200)
        sid = r.get_json()["session_id"]
        self.sids.append(sid)
        return sid

    def _say(self, sid, text, client=None):
        return (client or self.client).post("/api/chat/message", json={
            "session_id": sid, "content": text, "page_url": "https://optiwar.in/",
            "client_message_id": uuid.uuid4().hex})

    def _events(self, sid):
        cur = self.db.cursor()
        cur.execute("SELECT event_type, failure_code, payload FROM ai_events "
                    "WHERE session_id=%s ORDER BY created_at", (sid,))
        return cur.fetchall()

    def _row(self, sid):
        cur = self.db.cursor()
        cur.execute("SELECT customer_id, contact_email FROM chat_sessions WHERE session_id=%s",
                    (sid,))
        return cur.fetchone()

    def test_a_signed_out_browser_starts_its_own_chat(self):
        sid = self._start()
        row = self._row(sid)
        self.assertIsNone(row["customer_id"])
        self.assertFalse(row["contact_email"])
        opened = [e for e in self._events(sid) if e["event_type"] == "CONTACT_AI_OPENED"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(json.loads(opened[0]["payload"])["entry"], "contact_us")
        st = self.client.get("/api/chat/status").get_json()
        self.assertTrue(st["has_active"])
        self.assertEqual(st["session_id"], sid)
        self.assertEqual(self._start(), sid)          # the same browser resumes it

    def _adopted(self, sid):
        cur = self.db.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM chat_events WHERE session_id=%s "
                    "AND event_type='guest_session_adopted'", (sid,))
        return cur.fetchone()["n"]

    def test_a_chat_begun_signed_out_continues_after_sign_in(self):
        sid = self._start()
        self.assertEqual(self._say(sid, "scan my face").status_code, 200)
        self._sign_in()
        st = self.client.get("/api/chat/status").get_json()
        self.assertEqual(st["session_id"], sid)
        self.assertEqual(self._start(), sid)
        row = self._row(sid)
        self.assertEqual(row["customer_id"], 424242)
        self.assertEqual(row["contact_email"], "canary@example.com")
        self.assertEqual(self._adopted(sid), 1)
        msgs = self.client.get("/api/chat/messages/%s" % sid).get_json()["messages"]
        self.assertIn("scan my face", [m["content"] for m in msgs])

    def test_signing_in_on_another_browser_does_not_take_a_guest_chat(self):
        sid = self._start()
        other = self.app.test_client()
        with other.session_transaction() as s:
            s["user_id"] = 424242
            s["user_email"] = "canary@example.com"
            s["user_name"] = "Canary"
        st = other.get("/api/chat/status").get_json()
        self.assertNotEqual(st.get("session_id"), sid)
        self.assertIsNone(self._row(sid)["customer_id"])
        self.assertEqual(self._adopted(sid), 0)

    def test_an_adopted_chat_is_not_adopted_again_by_a_second_account(self):
        sid = self._start()
        self._sign_in()
        self.assertEqual(self.client.get("/api/chat/status").get_json()["session_id"], sid)
        with self.client.session_transaction() as s:
            s["user_id"] = 515151
            s["user_email"] = "second@example.com"
        st = self.client.get("/api/chat/status").get_json()
        self.assertNotEqual(st.get("session_id"), sid)
        self.assertEqual(self._row(sid)["customer_id"], 424242)
        self.assertEqual(self._adopted(sid), 1)

    def test_another_browser_cannot_use_or_see_an_anonymous_chat(self):
        sid = self._start()
        other = self.app.test_client()
        self.assertEqual(self._say(sid, "hello", client=other).status_code, 403)
        self.assertFalse(other.get("/api/chat/status").get_json()["has_active"])
        self.assertEqual(other.get("/api/chat/messages/%s" % sid).status_code, 403)

    def test_a_signed_out_callback_asks_for_contact_then_files_a_callback_ticket(self):
        sid = self._start()
        first = self._say(sid, "mujhe callback chahiye").get_json()
        self.assertEqual(self.prompts, [])                  # the server answered
        self.assertEqual(self.tickets, [])
        self.assertIn("mobile number", first["reply"])
        second = self._say(sid, "9810012345 test.customer@example.com").get_json()
        self.assertEqual(len(self.tickets), 1)
        t = self.tickets[0]
        self.assertEqual(t["phone"], "9810012345")
        self.assertEqual(t["email"], "test.customer@example.com")
        c = t["classification"]
        self.assertEqual((c["original_intent"], c["final_action"], c["ticket_reason"],
                          c["escalation_reason"], c["ai_failure_reason"]),
                         ("CALLBACK_REQUEST", "CREATE_TICKET", "CALLBACK_REQUEST",
                          "CUSTOMER_REQUESTED_HUMAN", None))
        self.assertIn("2345", second["reply"])
        self.assertIn("#77", second["reply"])
        self.assertNotIn("[ACTION:", second["reply"])
        self.assertFalse(self._row(sid)["contact_email"])  # still an anonymous chat
        raw = json.dumps([e["payload"] for e in self._events(sid)])
        self.assertNotIn("9810012345", raw)
        self.assertNotIn("test.customer@example.com", raw)
        types = [e["event_type"] for e in self._events(sid)]
        self.assertIn("TICKET_CREATED", types)
        self.assertIn("TICKET_CLASSIFIED", types)
        self.assertIn("ESCALATION_OFFERED", types)

    def test_a_signed_in_callback_is_filed_on_the_accounts_phone(self):
        self._sign_in()
        sid = self._start()
        body = self._say(sid, "mujhe koi call kar de").get_json()
        self.assertEqual(self.prompts, [])
        self.assertEqual(len(self.tickets), 1)
        self.assertEqual(self.tickets[0]["classification"]["ticket_reason"], "CALLBACK_REQUEST")
        self.assertIn("2345", body["reply"])

    def test_a_person_asked_for_is_a_ticket_with_the_customers_reason(self):
        self._sign_in()
        sid = self._start()
        self._say(sid, "I want to talk to a human")
        self.assertEqual(len(self.tickets), 1)
        c = self.tickets[0]["classification"]
        self.assertEqual((c["ticket_reason"], c["escalation_reason"]),
                         ("HUMAN_REQUEST", "CUSTOMER_REQUESTED_HUMAN"))

    def test_signed_out_account_questions_are_sent_to_sign_in_not_to_a_ticket(self):
        sid = self._start()
        for text in ("mera order kaha hai", "parcel return ho gaya hai", "payment ho gaya kya"):
            self.scripted.append(("Please sign in to see this.", None))
            body = self._say(sid, text).get_json()
            self.assertIn(order_lookup.SIGN_IN_ORDERS_URL, body["reply"], text)
            self.assertIn("NOT signed in", self.prompts[-1], text)
        self.assertEqual(self.tickets, [])

    def test_a_general_question_needs_no_sign_in(self):
        sid = self._start()
        self.scripted.append(("Returns are accepted within 14 days.", None))
        body = self._say(sid, "return policy kya hai").get_json()
        self.assertNotIn(order_lookup.SIGN_IN_ORDERS_URL, body["reply"])
        self.assertNotIn("NOT signed in", self.prompts[-1])

    def test_a_model_outage_answers_deterministically_and_says_so(self):
        sid = self._start()
        self.scripted.append((None, "provider_error"))
        body = self._say(sid, "mera order kaha hai").get_json()
        self.assertEqual(body["status"], "failed")
        self.assertIn(order_lookup.SIGN_IN_ORDERS_URL, body["reply"])
        self.assertEqual(self.tickets, [])
        fb = [e for e in self._events(sid) if e["event_type"] == "MODEL_FALLBACK_USED"]
        self.assertEqual(len(fb), 1)
        self.assertEqual(fb[0]["failure_code"], "MODEL_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
