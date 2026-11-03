"""A customer message and the reply to it often share one created_at second
(in production, 79 of 80 sessions over 30 days). Sorting on created_at alone
put the reply first in the model's history, in the ticket and KET transcript
and in the widget's reload. id breaks the tie, in insert order.
"""
import os
import re
import sys
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.test_chat_attachments import (  # noqa: E402
    AVAILABLE, CHAT_MESSAGES_DDL, _connect, _load_gateway)


class Source(unittest.TestCase):

    def test_every_created_at_order_on_chat_messages_breaks_ties_by_id(self):
        with open(os.path.join(REPO, "chat_gateway.py"), encoding="utf-8") as f:
            src = f.read()
        orders = re.findall(r"FROM chat_messages[^\"]*?ORDER BY created_at[^\"]*?LIMIT", src)
        self.assertGreaterEqual(len(orders), 4)
        for q in orders:
            self.assertRegex(q, r"ORDER BY created_at (ASC|DESC), id (ASC|DESC)")
            d = re.search(r"ORDER BY created_at (ASC|DESC), id (ASC|DESC)", q)
            self.assertEqual(d.group(1), d.group(2), q)


@unittest.skipUnless(AVAILABLE, "no MariaDB test database (see scripts/setup_test_db.sh)")
class OnMariaDB(unittest.TestCase):

    def setUp(self):
        self.cg = sys.modules.get("flaskr.chat_gateway") or _load_gateway()
        self.db = _connect()
        self.sid = "to_" + uuid.uuid4().hex[:12]
        cur = self.db.cursor()
        cur.execute(CHAT_MESSAGES_DDL)
        for role, source, text in (("user", "customer", "my frame arrived broken"),
                                   ("assistant", "ai", "Sorry to hear that. A ticket?"),
                                   ("user", "customer", "talk to a human"),
                                   ("assistant", "ai", "Support will reply within 24 hours.")):
            cur.execute("INSERT INTO chat_messages (session_id, source, role, content, status, "
                        "created_at) VALUES (%s, %s, %s, %s, 'sent', '2026-11-02 18:42:10')",
                        (self.sid, source, role, text))

    def tearDown(self):
        self.db.cursor().execute("DELETE FROM chat_messages WHERE session_id=%s", (self.sid,))
        self.db.close()

    def test_same_second_turns_keep_their_order_in_the_history(self):
        hist = self.cg._get_conversation_history(self.db, self.sid)
        self.assertEqual([m["role"] for m in hist], ["user", "assistant", "user", "assistant"])
        self.assertEqual(hist[0]["content"], "my frame arrived broken")

    def test_a_short_history_keeps_the_newest_turns(self):
        hist = self.cg._get_conversation_history(self.db, self.sid, limit=2)
        self.assertEqual([m["content"] for m in hist],
                         ["talk to a human", "Support will reply within 24 hours."])


if __name__ == "__main__":
    unittest.main()
