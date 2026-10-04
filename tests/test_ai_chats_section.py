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


def _sql(heads=HEADS, rows=ROWS):
    def sql(query):
        if "FROM chat_sessions" in query and "FROM chat_messages WHERE session_id" not in query:
            if isinstance(heads, Exception):
                raise heads
            return heads
        if isinstance(rows, Exception):
            raise rows
        return rows
    return sql


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


if __name__ == "__main__":
    unittest.main()
