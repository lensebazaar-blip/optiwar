"""Multilingual understanding (OPTIWA-1031).

A customer asked, in Hinglish, which power her glasses would be made with; the
Contact Us bot answered with a subject menu and filed a callback ticket. These
tests hold the server to: the language and script it detects, the canonical
intent every phrasing maps to (so one deterministic capability answers it),
what a ticket says the customer came for as opposed to what they last
clicked, and a prescription read only from the requester's own cart/orders.

    python3 -m unittest tests.test_multilingual_nlu
"""
import importlib.util
import json
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO, "tests", "multilingual_eval.json")


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name + "_under_test_ml", os.path.join(REPO, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


al = _load("ai_language")
rx = _load("rx_lookup")
acr = _load("acr")

with open(FIXTURE, encoding="utf-8") as fh:
    EVAL = json.load(fh)

INCIDENT = ["Hi",
            "Mene apna chashme k number add kiya hai mera chashma kis number ka bankar ayega",
            "Callback"]


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


class EvalSuiteTests(unittest.TestCase):

    def test_every_case_detects_and_classifies_as_recorded(self):
        for c in EVAL["cases"]:
            det = al.detect(c["text"])
            for key, got in (("language", det["language"]), ("script", det["script"]),
                             ("code_mixed", det["code_mixed"])):
                if key in c:
                    self.assertEqual(got, c[key], "%s %s: %r" % (c["id"], key, c["text"]))
            self.assertEqual(al.classify_intent(c["text"])[0], c["intent"],
                             "%s: %r" % (c["id"], c["text"]))

    def test_the_suite_covers_all_22_scheduled_languages(self):
        covered = {c["language"] for c in EVAL["cases"] if "language" in c}
        self.assertEqual(set(al.EIGHTH_SCHEDULE) - covered, set())
        self.assertEqual(len(al.EIGHTH_SCHEDULE), 22)

    def test_the_customer_is_followed_when_they_switch_language(self):
        for sw in EVAL["switches"]:
            self.assertEqual(al.conversation_language(sw["messages"])["language"],
                             sw["language"], sw["id"])

    def test_the_five_reported_phrasings_are_one_intent_and_one_capability(self):
        req = [c for c in EVAL["cases"] if c["id"].startswith("req-")]
        self.assertEqual(len(req), 5)
        for c in req:
            intent, conf = al.classify_intent(c["text"])
            self.assertEqual(intent, al.INTENT_PRESCRIPTION_CONFIRMATION, c["id"])
            self.assertGreaterEqual(conf, 0.75)
            self.assertEqual(al.INTENT_CAPABILITY[intent], "LOOKUP_PRESCRIPTION")

    def test_hindi_and_english_saved_prescription_reach_the_same_capability(self):
        a = al.classify_intent("mera saved number dikhao")[0]
        b = al.classify_intent("show my saved prescription")[0]
        self.assertEqual(a, b)
        self.assertEqual(al.INTENT_CAPABILITY[a], "LOOKUP_PRESCRIPTION")

    def test_an_order_number_is_not_a_prescription(self):
        self.assertEqual(al.classify_intent("mera order number kya hai")[0],
                         al.INTENT_ORDER_STATUS)

    def test_a_vague_message_is_low_confidence_and_names_no_intent(self):
        intent, conf = al.classify_intent("hmm ok")
        self.assertIn(intent, (al.INTENT_OTHER, al.INTENT_GREETING))
        u = al.understand(["hmm ok"])
        self.assertNotIn("Likely intent", al.prompt_section(u))
        self.assertLess(al.detect("")["confidence"], 0.1)

    def test_asking_which_phone_number_is_on_file_is_not_a_callback(self):
        for text in ("what phone number is on my account?", "change my phone number",
                     "mera phone number kya hai"):
            self.assertNotEqual(al.classify_intent(text)[0], al.INTENT_CALLBACK_REQUEST, text)
        for text in ("call me please", "please phone me", "mujhe phone karo", "Callback",
                     "mujhe call back chahiye"):
            self.assertEqual(al.classify_intent(text)[0], al.INTENT_CALLBACK_REQUEST, text)

    def test_an_account_question_is_not_a_payment_question(self):
        for text in ("what phone number is on my account?", "my account"):
            self.assertNotEqual(al.classify_intent(text)[0], al.INTENT_PAYMENT_STATUS, text)
        self.assertEqual(al.classify_intent("my amount was deducted")[0], al.INTENT_PAYMENT_STATUS)
        self.assertEqual(al.classify_intent("my amout was deducted")[0], al.INTENT_PAYMENT_STATUS)

    def test_asking_to_resend_a_parcel_is_a_reship_question(self):
        self.assertEqual(al.classify_intent("my parcel came back to you, can you resend it?")[0],
                         al.INTENT_RESHIP_STATUS)


class IncidentTests(unittest.TestCase):
    """OPTIWA-1031 itself: what she asked is kept apart from what she clicked."""

    def test_the_callback_click_does_not_replace_what_she_asked(self):
        u = al.understand(INCIDENT)
        self.assertEqual(u["intent"], al.INTENT_PRESCRIPTION_CONFIRMATION)
        self.assertEqual(u["capability"], "LOOKUP_PRESCRIPTION")
        self.assertEqual(u["detected_language"], "hi-Latn")

    def test_the_ticket_records_the_ai_failure_not_a_customer_callback(self):
        t = al.classify_ticket(INCIDENT, final_action="CREATE_TICKET",
                               ticket_reason=al.legacy_ticket_reason(
                                   "Requesting callback before ordering"))
        self.assertEqual(t["original_intent"], "PRESCRIPTION_CONFIRMATION")
        self.assertEqual(t["final_action"], "CREATE_TICKET")
        self.assertEqual(t["ticket_reason"], "CUSTOMER_CALLBACK")
        self.assertEqual(t["escalation_reason"], "LANGUAGE_UNDERSTANDING_FAILED")
        self.assertEqual(t["ai_failure_reason"], "NLU_LANGUAGE_FAILURE")

    def test_a_customer_who_asked_for_a_person_is_their_own_reason(self):
        t = al.classify_ticket(["mujhe kisi insaan se baat karni hai"])
        self.assertEqual(t["escalation_reason"], "CUSTOMER_REQUESTED_HUMAN")
        self.assertIsNone(t["ai_failure_reason"])

    def test_missing_data_and_a_down_tool_are_not_language_failures(self):
        en = ["Which power will my glasses be made in?"]
        self.assertEqual(al.classify_ticket(en, data_available=False)["escalation_reason"],
                         "DATA_MISSING")
        self.assertEqual(al.classify_ticket(en, tool_available=False)["escalation_reason"],
                         "TOOL_UNAVAILABLE")

    def test_the_models_stated_reason_wins_when_valid(self):
        t = al.classify_ticket(INCIDENT, model_reason="POLICY_REQUIRES_HUMAN")
        self.assertEqual(t["escalation_reason"], "POLICY_REQUIRES_HUMAN")
        t = al.classify_ticket(INCIDENT, model_reason="made-up")
        self.assertEqual(t["escalation_reason"], "LANGUAGE_UNDERSTANDING_FAILED")

    def test_no_classification_carries_the_customers_words(self):
        blob = json.dumps([al.understand(INCIDENT), al.classify_ticket(INCIDENT)])
        for word in ("chashma", "bankar", "Callback", "Hi"):
            self.assertNotIn(word, blob)


class MetaTagTests(unittest.TestCase):

    def test_the_tag_is_stripped_and_only_known_values_survive(self):
        reply, meta = al.extract_meta(
            "Aapka chashma usi power ka banega.\n"
            "[META:intent=PRESCRIPTION_CONFIRMATION;lang=hi-Latn;clarify=0;esc=]")
        self.assertEqual(reply, "Aapka chashma usi power ka banega.")
        self.assertEqual(meta, {"intent": "PRESCRIPTION_CONFIRMATION", "lang": "hi-Latn",
                                "clarify": False})

    def test_junk_in_the_tag_is_dropped_and_never_shown(self):
        reply, meta = al.extract_meta(
            "Hi [META:intent=DROP TABLE;lang=xx;esc=<script>;clarify=1] there")
        self.assertNotIn("META", reply)
        self.assertEqual(meta, {"clarify": True})

    def test_the_rules_demand_one_clarification_and_no_menu(self):
        rules = al.LANGUAGE_RULES
        self.assertIn("ONE short, specific clarification", rules)
        self.assertIn("Never answer an unclear question with a menu", rules)
        self.assertIn("never guess, round, calculate or invent a value", rules)
        self.assertIn("[META:intent=", rules)


class HindiConfirmationTests(unittest.TestCase):

    def test_haan_answers_a_yes_no_question(self):
        for yes in ("haan", "Haan ji", "ji", "theek hai", "हाँ", "जी", "ठीक है", "kar do"):
            self.assertTrue(acr.is_confirmation(yes), yes)
        for no in ("nahi", "hand", "have", "haan lekin pehle price batao"):
            self.assertFalse(acr.is_confirmation(no), no)


class _Cursor:
    def __init__(self, results):
        self.results = list(results)
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.results.pop(0) if self.results else []


class RxLookupTests(unittest.TestCase):

    def test_an_eye_is_quoted_exactly_as_stored(self):
        eye = rx.parse_eye("-1.25/-0.50/180/2.00")
        self.assertEqual(eye, {"sph": -1.25, "cyl": -0.5, "axis": 180.0, "add": 2.0})
        self.assertEqual(rx.format_eye(eye), "SPH -1.25 CYL -0.50 AXIS 180 ADD +2.00")
        self.assertEqual(rx.format_eye(rx.parse_eye("+0.75/0/0/")), "SPH +0.75")
        self.assertIsNone(rx.parse_eye("///"))
        self.assertEqual(rx.format_eye(None), "not entered")

    def test_a_cart_row_is_read_only_with_its_own_product(self):
        cart = [{"rx_id": 8224, "product_id": 506, "product_name": "Progressive frame"},
                {"rx_id": 1, "product_id": 999, "product_name": "Crafted"},
                {"rx_id": "x"}, "junk", {"product_id": 7}]
        cur = _Cursor([[{"rx_id": 8224, "product_id": 506,
                         "right_eye": "-1.00/-0.50/90/1.50", "left_eye": "-1.25///1.50",
                         "recommendations": "Progressive"},
                        {"rx_id": 1, "product_id": 312, "right_eye": "-9/0/0/0",
                         "left_eye": "-9/0/0/0", "recommendations": ""}]])
        m = rx.read_model(cur, cart, None)
        self.assertEqual(len(m["cart"]), 1)
        self.assertEqual(m["cart"][0]["right"]["sph"], -1.0)
        self.assertEqual(m["orders"], [])
        self.assertEqual(len(cur.executed), 1, "signed out: no order query at all")
        self.assertEqual(cur.executed[0][1], (8224, 1))

    def test_orders_are_the_signed_in_customers_on_this_site_only(self):
        cur = _Cursor([[{"order_id": "NZJYMR-947284", "date_created": "2026-09-29 18:29:52",
                         "right_eye": "-1.00/-0.50/90/1.50", "left_eye": "-1.25///1.50",
                         "recommendations": "Progressive", "product_name": "Frame",
                         "paid": 0}]])
        m = rx.read_model(cur, [], 4242, "in.optiwar.com")
        sql, params = cur.executed[0]
        self.assertIn("o.customer_id=%s", sql)
        self.assertIn("o.site_from=%s", sql)
        self.assertIn("o.is_test=0", sql)
        self.assertEqual(params, (4242, "in.optiwar.com"))
        self.assertFalse(m["orders"][0]["paid"])
        section = rx.prompt_section(m, True)
        self.assertIn("SPH -1.00 CYL -0.50 AXIS 90 ADD +1.50", section)
        self.assertIn("payment NOT completed", section)

    def test_nothing_on_file_is_said_and_counted_without_values(self):
        m = rx.read_model(_Cursor([]), [], None)
        self.assertFalse(rx.found(m))
        self.assertIn("Nothing on file", rx.prompt_section(m, False))
        self.assertIn("not signed in", rx.prompt_section(m, False))
        payload = rx.event_payload({"cart": [{"right": {"sph": -1.0}}], "orders": []}, True)
        self.assertEqual(payload, {"cart_lines": 1, "order_lines": 0, "unpaid_orders": 0,
                                   "signed_in": True, "found": True})


class WiringTests(unittest.TestCase):
    """The places production reaches these modules, by source."""

    @classmethod
    def setUpClass(cls):
        cls.gw = _read("chat_gateway.py")
        cls.crm = _read("crm.py")

    def _body(self, src, name):
        body = src[src.index("def %s(" % name):]
        return body[:body.index("\ndef ", 10)]

    def test_the_lookup_is_the_browsers_cart_and_login(self):
        body = self._body(self.gw, "_rx_context")
        self.assertIn("flask_session.get('user_id')", body)
        self.assertIn("flask_session.get('cart')", body)
        self.assertNotIn("data.get(", body)
        self.assertNotIn("session.get('customer_id')", body)

    def test_the_rules_and_the_turn_note_reach_the_model(self):
        self.assertIn("ai_language.LANGUAGE_RULES + ''.join(extra_sections)", self.gw)
        self.assertIn("system_prompt += (ai_language.prompt_section(understanding) + rx_section"
                      " + order_section", self.gw)

    def test_the_tag_is_removed_before_anything_reads_the_reply(self):
        strip = self.gw.index("ai_reply, turn_meta = ai_language.extract_meta(ai_reply)")
        self.assertLess(strip, self.gw.index("ai_reply, lens_proposal = _park_lens_proposal("))
        self.assertLess(strip, self.gw.index("_insert_message(db, session_id, 'ai', 'assistant', ai_reply"))

    def test_every_turn_and_every_ticket_is_classified(self):
        self.assertIn("acr.EV_TURN_UNDERSTOOD", self.gw)
        self.assertIn("acr.EV_TICKET_CLASSIFIED", self.gw)
        self.assertIn("acr.EV_LEGACY_SUPPORT_ROUTE", self.crm)
        self.assertIn("acr.EV_PRESCRIPTION_LOOKUP", self.gw)

    def test_contact_us_opens_the_assistant_and_no_ticket_form(self):
        html = _read("templates/base.html")
        self.assertIn('window.owChatOpen("text",entry||"contact_us")', html)
        self.assertIn('function openContactChoice(entry){owSupportOpen(', html)
        self.assertIn('function openContactModal(){owSupportOpen("order_support");}', html)
        for form in ('id="contactModal"', 'id="contactForm"', 'id="cSubject"', "Select a subject",
                     "Requesting callback", "Submit Ticket", "/contact_us/ai_chat",
                     "/contact_us/submit", "/contact_us/captcha"):
            self.assertNotIn(form, html, form)

    def test_the_fallback_bot_no_longer_forces_a_subject_menu(self):
        body = self._body(self.crm, "ai_chat")
        self.assertNotIn("pick closest", body)
        self.assertNotIn("OpenAI", body)
        self.assertNotIn("gpt-3.5", self.crm)
        self.assertIn("_legacy_support_route('contact_us/ai_chat')", body)
        self.assertIn('"ticket_data": None', body)

    def test_deployable(self):
        manifest = _read(os.path.join("deploy", "deploy.py"))
        for name in ("ai_language.py", "rx_lookup.py", "order_lookup.py"):
            self.assertEqual(manifest.count('"%s"' % name), 2, name)


if __name__ == "__main__":
    unittest.main()
