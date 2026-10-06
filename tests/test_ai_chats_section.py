import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reports import ai_chats_section as acs  # noqa: E402
from reports.report_db import SqlError  # noqa: E402


def _hex(s):
    return s.encode("utf-8").hex().upper()


HEADS = [("chat_a", "42", _hex("Asha Rao"), _hex("asha@example.com"), "resolved", "KET-9",
          _hex("https://optiwar.in/cart")),
         ("chat_b", "", "", "", "active", "", "")]
ROWS = [
    ("chat_a", "customer", _hex("Where is my order?\nPhone 9810011111\tthanks"), "",
     "2026-10-03 10:00:00"),
    ("chat_a", "ai", _hex("It ships <today> & arrives soon."), _hex('{"actions": ["navigate"]}'),
     "2026-10-03 10:00:05"),
    ("chat_a", "human", _hex("Agent here."), _hex('{"actions": []}'), "2026-10-03 10:05:00"),
    ("chat_b", "customer", _hex("नमस्ते"), "", "2026-10-03 09:00:00"),
]


def _sql(heads=HEADS, rows=ROWS, events=(), actions=(), orders=(), phones=(), test_ids=()):
    def sql(query):
        if "is_test=1" in query:
            if isinstance(test_ids, Exception):
                raise test_ids
            return [(i,) for i in test_ids]
        for table, data in (("FROM ai_events", events), ("FROM ai_actions", actions),
                            ("FROM ai_session_commerce", orders), ("FROM customers", phones)):
            if table in query:
                if isinstance(data, Exception):
                    raise data
                return list(data)
        if "FROM chat_sessions" in query and "FROM chat_messages WHERE session_id" not in query:
            if isinstance(heads, Exception):
                raise heads
            return heads
        if isinstance(rows, Exception):
            raise rows
        return rows
    return sql


def _ev(sid, etype, at, payload=None, action_id="", action_type="", success="1", stage="",
        provider="", model="", ms=""):
    import json
    return (sid, etype, at, action_id, action_type, success, "", stage, provider, model, ms,
            _hex(json.dumps(payload)) if payload else "")


TRACE = {"v": 1, "source": "MODEL + TOOL", "trigger": "customer_message", "language": "en",
         "intent": "PRODUCT_SEARCH", "confidence": 0.9,
         "tools": [{"tool": "search_products", "args": {"color": "black", "shape": "round"},
                    "matched": 17, "returned": 15, "skus": ["SKU1", "SKU2"],
                    "ranking": "in_stock_qty_desc_v1", "catalog_at": "2026-10-03 06:00:00"}],
         "model_calls": [{"provider": "deepseek", "model": "deepseek-chat", "ok": True,
                          "ms": 900, "in": 1200, "out": 80}],
         "page": {"kind": "listing", "site": "optiwar.in", "facts_used": []},
         "action": {"id": "act-1", "type": "NAVIGATE", "state": "OFFERED",
                    "target": "/frames?color=black"}}


class AiChatsSectionTests(unittest.TestCase):
    def test_every_chat_is_read_whole_and_in_order(self):
        sessions, errors = acs.collect(_sql())
        self.assertEqual(errors, [])
        self.assertEqual([s["session_id"] for s in sessions], ["chat_b", "chat_a"])
        a = sessions[1]
        self.assertEqual((a["name"], a["email"], a["customer_id"], a["ket_ref"]),
                         ("Asha Rao", "asha@example.com", "42", "KET-9"))
        self.assertEqual(a["messages"][0]["text"], "Where is my order?\nPhone 9810011111\tthanks")
        self.assertEqual(sessions[0]["messages"][0]["text"], "नमस्ते")
        self.assertEqual(acs.summary(sessions), {"chats": 2, "messages": 4, "customer_messages": 2,
                                                 "signed_in": 1, "escalated": 1, "agent_messages": 1})

    def test_the_html_shows_the_transcript_as_written_and_escaped(self):
        sessions, _ = acs.collect(_sql())
        page = acs.render_html(sessions, "2026-10-04")
        self.assertIn("Asha Rao · asha@example.com · customer #42", page)
        self.assertIn("Phone 9810011111\tthanks", page)
        self.assertIn("It ships &lt;today&gt; &amp; arrives soon.", page)
        self.assertNotIn("<today>", page)
        self.assertIn('{&quot;actions&quot;: [&quot;navigate&quot;]}', page)
        self.assertIn("KET KET-9", page)
        self.assertIn("guest", page)
        self.assertLess(page.index("नमस्ते"), page.index("Where is my order?"))
        self.assertEqual(page.count('class="chat"'), 2)
        self.assertNotIn("&quot;actions&quot;: []", page)

    def test_the_body_summary_names_the_attachment(self):
        sessions, _ = acs.collect(_sql())
        text = acs.build(sessions, acs.file_name("2026-10-04"))
        self.assertIn("AI CHATS (last 24h)", text)
        self.assertIn("Chats                         2", text)
        self.assertIn("Escalated to KET              1", text)
        self.assertIn("ai_chats_2026-10-04.html", text)
        self.assertIn("none (no chats)", acs.build([], "x.html"))

    def test_a_read_failure_is_reported_not_raised(self):
        sessions, errors = acs.collect(_sql(rows=SqlError("denied")))
        self.assertEqual(sessions, [])
        self.assertEqual(errors, ["chat_messages: denied"])
        sessions, errors = acs.collect(_sql(heads=SqlError("denied")))
        self.assertEqual(len(sessions), 2)
        self.assertEqual(errors, ["chat_sessions: denied"])
        self.assertIn("! chat_sessions: denied", acs.build(sessions, "x.html", errors))

    def test_main_writes_a_private_file_and_removes_a_stale_one(self):
        with tempfile.TemporaryDirectory() as d:
            real_dir, real_collect = acs.REPORT_DIR, acs.collect
            acs.REPORT_DIR = d
            try:
                acs.collect = lambda: real_collect(_sql())
                acs.main("2026-10-04")
                path = os.path.join(d, "ai_chats_2026-10-04.html")
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                acs.collect = lambda: ([], [])
                acs.main("2026-10-04")
                self.assertFalse(os.path.exists(path))
            finally:
                acs.REPORT_DIR, acs.collect = real_dir, real_collect


    def test_a_signed_in_chat_shows_the_account_phone_as_verified(self):
        sessions, errors = acs.collect(_sql(phones=[("42", _hex("+919810022222"))]))
        self.assertEqual(errors, [])
        a = dict(acs.identity(sessions[1]))
        self.assertEqual((a["Phone"], a["Phone source"]), ("+919810022222", "ACCOUNT_VERIFIED"))
        self.assertEqual(a["Signed in"], "YES")
        self.assertEqual(a["Provided during chat"], "9810011111 (CHAT_PROVIDED)")
        g = dict(acs.identity(sessions[0]))
        self.assertEqual((g["Customer"], g["Phone"], g["Signed in"]), ("Guest", "not known", "NO"))

    def test_without_the_customers_grant_the_phone_says_so(self):
        sessions, errors = acs.collect(_sql(phones=SqlError("denied")))
        self.assertEqual(errors, [])
        self.assertIn("no grant", dict(acs.identity(sessions[1]))["Phone"])

    def test_a_stored_trace_is_rendered_without_hidden_fields(self):
        rows = [("chat_c", "customer", _hex("black round frames"), "", "2026-10-03 11:00:00"),
                ("chat_c", "ai", _hex("Here are some."), _hex(__import__("json").dumps(
                    {"trace": TRACE, "actions": ["navigate"]})), "2026-10-03 11:00:03")]
        heads = [("chat_c", "", "", "", "active", "", _hex("https://optiwar.in/frames"))]
        sessions, _ = acs.collect(_sql(heads=heads, rows=rows))
        page = acs.render_html(sessions, "2026-10-04")
        self.assertIn("Source: AI + TOOL", page)
        self.assertIn("filters color=black, shape=round · matched 17 · returned 15", page)
        self.assertIn("ranking in_stock_qty_desc_v1", page)
        self.assertIn("deepseek · requested deepseek-chat · returned NOT REPORTED · 1 call(s) · "
                      "tokens in 1200 / out 80", page)
        self.assertIn("Action NAVIGATE OFFERED · act-1", page)
        self.assertNotIn("&quot;trace&quot;", page)
        h = acs.headline(sessions)
        self.assertEqual((h["model_replies"], h["tool_replies"], h["real"]), (1, 1, 1))

    def test_a_turn_without_a_stored_trace_is_rebuilt_from_its_events(self):
        rows = [("chat_d", "ai", _hex("Hi! How can I help?"), "", "2026-10-03 12:00:00"),
                ("chat_d", "customer", _hex("blue frames"), "", "2026-10-03 12:00:10"),
                ("chat_d", "ai", _hex("Sure."), "", "2026-10-03 12:00:14"),
                ("chat_d", "customer", _hex("yes"), "", "2026-10-03 12:00:30"),
                ("chat_d", "ai", _hex("Opening."), "", "2026-10-03 12:00:31")]
        events = [_ev("chat_d", "SESSION_STARTED", "2026-10-03 12:00:00", {"authenticated": False}),
                  _ev("chat_d", "TURN_UNDERSTOOD", "2026-10-03 12:00:10",
                      {"detected_language": "en", "turn_intent": "PRODUCT_SEARCH"}),
                  _ev("chat_d", "MODEL_CALL", "2026-10-03 12:00:12", {"input_tokens": 500,
                      "output_tokens": 40, "actual_model": "deepseek-flash"},
                      provider="deepseek", model="deepseek-chat", ms="800"),
                  _ev("chat_d", "MODEL_CALL", "2026-10-03 12:00:13", {"output_tokens": 9},
                      provider="deepseek", model="deepseek-chat", success="0", ms="300"),
                  _ev("chat_d", "RECOMMENDATION_GENERATED", "2026-10-03 12:00:14",
                      {"result_count": 3, "skus": ["A", "B", "C"], "filters": {"color": "blue"}}),
                  _ev("chat_d", "ACTION_EXECUTED", "2026-10-03 12:00:35", action_id="act-9"),
                  _ev("chat_d", "JOURNEY_STAGE", "2026-10-03 12:00:36", stage="LISTING")]
        heads = [("chat_d", "", "", "", "active", "", "")]
        sessions, _ = acs.collect(_sql(heads=heads, rows=rows, events=events))
        s = sessions[0]
        greet, first, second = (acs.trace_for(s, i) for i in (0, 2, 4))
        self.assertEqual(greet["basis"], "greeting")
        self.assertIn("Source: RULE · widget greeting", acs.render_html([s], "2026-10-04"))
        self.assertEqual((first["source"], first["language"], first["intent"]),
                         ("MODEL + TOOL", "en", "PRODUCT_SEARCH"))
        self.assertEqual(first["tools"][0]["returned"], 3)
        self.assertEqual([c["tool_call"] for c in first["model_calls"]], [False, True])
        model_line = [x for x in acs.trace_lines(first) if x.startswith("Model:")][0]
        self.assertIn("requested deepseek-chat · returned deepseek-flash", model_line)
        self.assertNotIn("failed", model_line)
        self.assertEqual(second["source"], "DETERMINISTIC")
        self.assertTrue(acs.trace_lines({"source": "DETERMINISTIC", "trigger": "action_confirmation"})[0]
                        .startswith("Source: DETERMINISTIC · Trigger: action_confirmation"))
        self.assertTrue(acs.trace_lines({"source": "DETERMINISTIC", "trigger": "session_start"})[0]
                        .startswith("Source: RULE"))
        out = dict(acs.outcome(s)[0])
        self.assertEqual((out["Actions executed"], out["Furthest page reached"]), ("1", "LISTING"))

    def test_a_last_row_that_lost_its_trailing_empty_column_is_read(self):
        heads = [("chat_a", "42", _hex("Asha Rao"), _hex("asha@example.com"), "resolved", "KET-9")]
        rows = [("chat_a", "customer", _hex("hi"), "", "2026-10-03 10:00:00")]
        sessions, errors = acs.collect(_sql(heads=heads, rows=rows, phones=[("42",)]))
        self.assertEqual(errors, [])
        a = dict(acs.identity(sessions[0]))
        self.assertEqual((a["Phone"], a["Current page"]), ("not on the account", "-"))
        self.assertNotIn("Phone source", a)

    def test_a_reconstructed_offer_is_offered_on_its_turn_and_confirmed_on_the_yes(self):
        rows = [("chat_e", "customer", _hex("aviators"), "", "2026-10-03 12:00:10"),
                ("chat_e", "ai", _hex("Shall I open them?"), "", "2026-10-03 12:00:14"),
                ("chat_e", "customer", _hex("yes"), "", "2026-10-03 12:00:30"),
                ("chat_e", "ai", _hex("Opening."), "", "2026-10-03 12:00:31")]
        events = [_ev("chat_e", "NAVIGATION_OFFERED", "2026-10-03 12:00:14", action_id="act-5",
                      action_type="NAVIGATE"),
                  _ev("chat_e", "ACTION_CONFIRMED", "2026-10-03 12:00:30", action_id="act-5",
                      action_type="NAVIGATE"),
                  _ev("chat_e", "ACTION_EXECUTED", "2026-10-03 12:00:31", action_id="act-5",
                      action_type="NAVIGATE")]
        heads = [("chat_e", "", "", "", "active", "", "")]
        s = acs.collect(_sql(heads=heads, rows=rows, events=events))[0][0]
        first, second = acs.trace_for(s, 1), acs.trace_for(s, 3)
        self.assertEqual((first["action"]["state"], first["basis"]), ("OFFERED", acs.HISTORICAL))
        self.assertEqual((second["action"]["id"], second["action"]["state"]), ("act-5", "EXECUTED"))
        self.assertIn("HISTORICAL RECONSTRUCTION", acs.render_html([s], "2026-10-04"))

    def test_sessions_are_classified(self):
        def one(rows, email="", cid="", events=(), actions=()):
            heads = [("x", cid, "", _hex(email) if email else "", "active", "", "")]
            return acs.collect(_sql(heads=heads, rows=rows, events=events,
                                    actions=actions))[0][0]["kind"]
        greet = [("x", "ai", _hex("Hi"), "", "2026-10-03 12:00:00")]
        talk = greet + [("x", "customer", _hex("hello"), "", "2026-10-03 12:00:05")]
        start = lambda flag: [_ev("x", "SESSION_STARTED", "2026-10-03 12:00:00",  # noqa: E731
                                  {"authenticated": False, "acr_canary": flag})]
        act = [("x", "a1", "NAVIGATE", _hex("/frames"), "EXECUTED", "", "2026-10-03 12:00:06",
                "", "")]
        self.assertEqual(one(greet), acs.WIDGET)
        self.assertEqual(one(talk), acs.REAL)
        self.assertEqual(one(talk, events=start(True)), acs.CANARY)
        self.assertEqual(one(talk, email="ops@optiwar.com", cid="7"), acs.TEST)
        heads = [("x", "", "", _hex("walkin@example.com"), "active", "", "")]
        guest = acs.collect(_sql(heads=heads, rows=talk))[0][0]
        self.assertEqual(dict(acs.identity(guest))["Email"], "walkin@example.com (CHAT_PROVIDED)")
        self.assertEqual(one(talk, actions=act), acs.CANARY)
        self.assertEqual(one(talk, events=start(False), actions=act), acs.REAL)

    def test_a_flagged_test_account_is_test_and_an_ungranted_flag_falls_back(self):
        heads = [("chat_a", "42", _hex("Q A"), _hex("qa@example.invalid"), "active", "", "")]
        rows = [("chat_a", "customer", _hex("where is my order?"), "", "2026-10-03 10:00:00")]
        s = acs.collect(_sql(heads=heads, rows=rows, test_ids=["42"]))[0][0]
        self.assertEqual((s["kind"], s["kind_basis"]), (acs.TEST, "TEST account (customers.is_test)"))
        s = acs.collect(_sql(heads=heads, rows=rows, test_ids=SqlError("denied")))[0][0]
        self.assertEqual(s["kind"], acs.REAL)

    def test_the_body_carries_counts_only(self):
        sessions, _ = acs.collect(_sql(phones=[("42", _hex("+919810022222"))]))
        text = acs.build(sessions, acs.file_name("2026-10-04"))
        self.assertIn("Sessions with customer input  2", text)
        for pii in ("Asha", "asha@", "9810022222", "9810011111"):
            self.assertNotIn(pii, text)


if __name__ == "__main__":
    unittest.main()
