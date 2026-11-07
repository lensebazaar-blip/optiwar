"""The old /search chat is retired: every chat is the assistant.

The old chat kept the conversation only in the browser session, so a ticket
it filed reached support with a fixed line, no transcript and no KET ticket
(ticket 116). /search and /chat now land on a page with the assistant open,
and a cached old page gets a reply that calls no model and files nothing.
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _route(src, name):
    body = src[src.index("def %s(" % name):]
    nxt = re.search(r"\n@bp\.route|\ndef ", body[1:])
    return body[:nxt.start() + 1] if nxt else body


class SearchChatRetired(unittest.TestCase):

    def test_the_old_search_page_is_gone(self):
        self.assertFalse(os.path.exists(os.path.join(REPO, "templates", "search.html")))
        tdir = os.path.join(REPO, "templates")
        for root, _dirs, files in os.walk(tdir):
            for name in files:
                if name.endswith(".html"):
                    html = _read(os.path.join(root, name))
                    for old in ("/chat/api/message", "/chat/api/contact-request",
                                "Customer requested support ticket via chat."):
                        self.assertNotIn(old, html, "%s: %s" % (name, old))

    def test_search_opens_the_assistant(self):
        fn = _route(_read("models.py"), "search_products")
        self.assertIn('target = "/?support=legacy_search"', fn)
        self.assertIn("return redirect(target)", fn)
        self.assertIn("EV_LEGACY_SUPPORT_ROUTE", fn)
        self.assertNotIn("render_template", fn)
        self.assertNotIn("extract_search_intent", fn)

    def _search_redirect(self, args):
        import textwrap
        import types
        from urllib.parse import urlencode
        src = textwrap.dedent(_route(_read("models.py"), "search_products"))
        events = []

        class _Db:
            def commit(self):
                pass

        ns = {
            "request": types.SimpleNamespace(args=args, method="GET"),
            "redirect": lambda url: url,
            "get_db": _Db,
            "urlencode": urlencode,
            "current_app": types.SimpleNamespace(logger=types.SimpleNamespace(warning=print)),
            "acr": types.SimpleNamespace(
                EV_LEGACY_SUPPORT_ROUTE="LEGACY_SUPPORT_ROUTE", STAGE_SUPPORT="support",
                log_event=lambda db, ev, journey_stage, payload: events.append(payload)),
        }
        exec(src, ns)
        return ns["search_products"](), events

    def test_the_search_words_are_carried_into_the_assistant(self):
        url, events = self._search_redirect({"q": "  gold   round "})
        self.assertEqual(url, "/?support=legacy_search&q=gold+round")
        self.assertEqual(events, [{"route": "search", "method": "GET", "has_query": True}])
        url, _ = self._search_redirect({"query": "cat-eye & red"})
        self.assertEqual(url, "/?support=legacy_search&q=cat-eye+%26+red")
        url, events = self._search_redirect({})
        self.assertEqual(url, "/?support=legacy_search")
        self.assertFalse(events[0]["has_query"])
        url, _ = self._search_redirect({"q": "x" * 500})
        self.assertEqual(url, "/?support=legacy_search&q=" + "x" * 200)

    def test_the_widget_types_the_carried_words_without_sending(self):
        js = _read("static/js/chat-widget.js")
        block = js[js.index("if (supportEntry) {"):js.index("} else if (window.location.hash === '#owchat')")]
        self.assertIn("get('q')", block)
        self.assertIn("input.value = carried", block)
        self.assertNotIn("sendMessage", block)
        self.assertNotIn("innerHTML", block)
        # The words leave the address bar before the chat opens, so the
        # page_url the session, events and attachments store never has them.
        strip = block.index("history.replaceState(")
        self.assertIn("params.delete('q')", block[:strip])
        self.assertLess(strip, block.index("window.owChatOpen('text', supportEntry)"))

    def test_old_chat_routes_call_no_model_and_file_nothing(self):
        chat = _read("chat.py")
        self.assertIn("return redirect(ASSISTANT_URL)", _route(chat, "chat_page"))
        for name in ("chat_message", "contact_request"):
            fn = _route(chat, name)
            for call in ("call_model", "create_ticket_in_db", "send_contact_email",
                         "notify_support_ticket_created", "_get_chat_session"):
                self.assertNotIn(call, fn, "%s: %s" % (name, call))
        self.assertIn("'action': 'timeout'", _route(chat, "chat_message"))

    def test_header_and_footer_search_open_the_chat_in_place(self):
        base = _read("templates/base.html")
        self.assertIn("window.owChatOpen('text', 'header_search')", base)
        self.assertIn("window.owChatOpen('text', 'footer_search')", base)

    def test_the_resolved_ticket_email_links_to_the_assistant(self):
        self.assertIn("/?support=ticket_resolved", _read("notifications.py"))
        self.assertNotIn("{site_host}/search\"", _read("notifications.py"))


if __name__ == "__main__":
    unittest.main()
