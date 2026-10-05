"""Every assistant reply stores a safe operational trace in its message
metadata: which path answered, what the model and tools did, and the action
it carried. The daily AI chats report renders it; nothing in it is prompt
text, model reasoning or customer data."""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.test_chat_attachments import _load_gateway  # noqa: E402


class TurnTrace(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.cg = sys.modules.get("flaskr.chat_gateway") or _load_gateway()

    def test_response_source(self):
        rs = self.cg._response_source
        self.assertEqual(rs("model", [], False), "MODEL")
        self.assertEqual(rs("model", [{"tool": "search_products"}], False), "MODEL + TOOL")
        self.assertEqual(rs("rule", [], False), "DETERMINISTIC")
        self.assertEqual(rs("fallback", [], False), "FALLBACK")
        self.assertEqual(rs("model", [], True), "SUPPORT/KET")

    def test_page_kind(self):
        pk = self.cg._page_kind
        self.assertIsNone(pk(""))
        self.assertEqual(pk("https://optiwar.in/"), "home")
        self.assertEqual(pk("https://optiwar.in/product?pid=12"), "product")
        self.assertEqual(pk("https://optiwar.com/cart/"), "cart")
        self.assertEqual(pk("https://optiwar.in/frames/men"), "listing")

    def test_search_args_keep_the_keyword_and_drop_empties(self):
        self.assertEqual(self.cg._search_args({"color": "Black", "shape": "", "keyword": "BP86",
                                               "limit": 5}),
                         {"color": "Black", "keyword": "BP86"})

    def test_the_trace_holds_facts_about_the_turn_only(self):
        calls = [{"kind": "model_call", "provider": "deepseek", "model": "deepseek-chat",
                  "success": False, "tool_call": True, "duration_ms": 700,
                  "input_tokens": 1500, "output_tokens": 30, "request_id": "r1"},
                 {"kind": "model_call", "provider": "deepseek", "model": "deepseek-chat",
                  "success": True, "duration_ms": 900, "input_tokens": 1900,
                  "output_tokens": 120}]
        tools = [{"tool": "search_products", "args": {"color": "black"}, "returned": 4}]
        t = self.cg._turn_trace("model", {"detected_language": "hi", "intent_confidence": 0.8},
                                "PRODUCT_SEARCH", tools, calls, "https://optiwar.in/frames",
                                True, action={"type": "NAVIGATE", "state": "OFFERED"})
        self.assertEqual((t["source"], t["trigger"], t["language"], t["intent"]),
                         ("MODEL + TOOL", "customer_message", "hi", "PRODUCT_SEARCH"))
        self.assertEqual(t["page"], {"kind": "listing", "site": "optiwar.in", "facts_used": []})
        self.assertEqual(t["model_calls"][0], {"kind": "model_call", "provider": "deepseek",
                                               "model": "deepseek-chat", "ok": False,
                                               "tool_call": True, "ms": 700, "in": 1500,
                                               "out": 30})
        self.assertNotIn("request_id", str(t))
        self.assertEqual(t["action"]["state"], "OFFERED")


if __name__ == "__main__":
    unittest.main()
