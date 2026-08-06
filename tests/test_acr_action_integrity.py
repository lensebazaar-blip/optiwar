"""Tests for ACR A1/A2 pure decision logic (acr.py).

These cover the heart of the Action-Integrity fix without needing Flask or a DB:

  - bare confirmations ("yes", "take me there") are recognised so they can be
    resolved against a pending action instead of re-inferred from the word;
  - non-confirmations (real questions) are NOT treated as confirmations;
  - promise-without-action phrases are detected (the "AI lied" case);
  - the mandatory fallback link is appended once, with a sensible label.

    python3 -m unittest tests.test_acr_action_integrity
"""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_acr():
    # acr.py only uses the stdlib, so load it directly by path (no package deps).
    spec = importlib.util.spec_from_file_location("acr_under_test", os.path.join(REPO, "acr.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


acr = _load_acr()


class ConfirmationTests(unittest.TestCase):
    def test_bare_affirmatives_are_confirmations(self):
        for t in ["yes", "Yes", "yes!", "yeah", "yep", "sure", "ok", "okay",
                  "go", "go ahead", "proceed", "take me there", "open them",
                  "show me", "sounds good", "absolutely", "let's go"]:
            self.assertTrue(acr.is_confirmation(t), t)

    def test_questions_and_statements_are_not_confirmations(self):
        for t in ["no", "not now", "what colours do you have?",
                  "do you have these in black?", "how much is it",
                  "yes but only if it's under 50 euros and blue",
                  "can you show me something cheaper"]:
            self.assertFalse(acr.is_confirmation(t), t)

    def test_empty_is_not_confirmation(self):
        self.assertFalse(acr.is_confirmation(""))
        self.assertFalse(acr.is_confirmation(None))


class PromiseDetectionTests(unittest.TestCase):
    def test_promise_phrases_detected(self):
        for t in ["Let me take you there!", "Taking you there now",
                  "I've opened the frames for you", "Opening them now",
                  "I'll take you to the page"]:
            self.assertTrue(acr.promises_navigation(t), t)

    def test_non_promises_not_flagged(self):
        for t in ["Would you like me to take you to these frames?",
                  "Here are 4 frames that suit you.",
                  "Shall I open them for you? Yes or No",
                  "Want me to take you there?",
                  "Do you want me to open the frames page?"]:
            self.assertFalse(acr.promises_navigation(t), t)

    def test_offer_containing_promise_words_is_not_a_promise(self):
        # An offer that literally contains "take you there" must NOT auto-fire
        # a navigation — the customer hasn't confirmed yet.
        self.assertFalse(
            acr.promises_navigation("Would you like me to take you there?"))
        # But the assertive claim on the confirmation turn IS a promise.
        self.assertTrue(acr.promises_navigation("Great — taking you there now!"))


class NavigationOfferTests(unittest.TestCase):
    def test_navigation_offers_detected(self):
        for t in ["Would you like me to take you to these frames?",
                  "Shall I open them for you?",
                  "Want me to take you there?",
                  "Do you want me to open the frames page?"]:
            self.assertTrue(acr.offers_navigation(t), t)

    def test_non_navigation_offers_not_detected(self):
        # These are offers, but NOT to navigate — a later bare "yes" here must
        # not seed / resolve a navigation (the ticket/handover redirect bug).
        for t in ["Do you want me to connect you with my supervisor? Yes or No",
                  "Would you like me to create a support ticket for this? Yes or No",
                  "Here are 2 frames that suit you.",
                  "Shall I email you the receipt?"]:
            self.assertFalse(acr.offers_navigation(t), t)

    def test_empty_is_not_an_offer(self):
        self.assertFalse(acr.offers_navigation(""))
        self.assertFalse(acr.offers_navigation(None))


class BestEffortActionTests(unittest.TestCase):
    class _BoomCursor:
        def execute(self, *a, **k):
            raise RuntimeError("table missing")

        def fetchone(self):
            raise RuntimeError("table missing")

    class _BoomDB:
        def cursor(self):
            return BestEffortActionTests._BoomCursor()

    def test_action_helpers_never_raise_when_tables_missing(self):
        db = self._BoomDB()
        # A missing ai_actions table must degrade gracefully, never 500 the chat.
        self.assertIsNone(acr.create_pending_action(db, "s1", "NAVIGATE", "/x"))
        self.assertIsNone(acr.get_live_pending_action(db, "s1", "NAVIGATE"))
        self.assertFalse(acr.mark_action(db, "a1", "CONFIRMED"))
        self.assertFalse(acr.record_action_result(db, "a1", True))

    def test_mark_action_noop_on_empty_id(self):
        self.assertFalse(acr.mark_action(self._BoomDB(), None, "CONFIRMED"))


class FilteredListingUrlTests(unittest.TestCase):
    def test_no_filters_falls_back_to_generic_listing(self):
        self.assertEqual(acr.filtered_listing_url(None), acr.FRAMES_LISTING_FALLBACK)
        self.assertEqual(acr.filtered_listing_url({}), acr.FRAMES_LISTING_FALLBACK)
        self.assertEqual(acr.filtered_listing_url({'color': ''}), acr.FRAMES_LISTING_FALLBACK)

    def test_filters_preserve_recommendation_identity(self):
        url = acr.filtered_listing_url({'color': 'black', 'facefit': 'medium'})
        self.assertTrue(url.startswith(acr.FRAMES_LISTING_FALLBACK + '?'))
        self.assertIn('color=black', url)
        self.assertIn('facefit=medium', url)

    def test_filter_order_is_stable(self):
        url = acr.filtered_listing_url({'facefit': 'large', 'color': 'blue'})
        # color precedes facefit per NAV_FILTER_KEYS regardless of input order
        self.assertLess(url.index('color=blue'), url.index('facefit=large'))


class FallbackLinkTests(unittest.TestCase):
    def test_appends_button_once(self):
        r1 = acr.with_fallback_link("Here are your frames.", "/eyeglasses/all-spectacle-frames.html")
        self.assertIn("](/eyeglasses/all-spectacle-frames.html)", r1)
        self.assertTrue(r1.strip().rsplit("\n", 1)[-1].startswith("[\u25b6"))
        # idempotent: same link not duplicated
        r2 = acr.with_fallback_link(r1, "/eyeglasses/all-spectacle-frames.html")
        self.assertEqual(r1, r2)

    def test_empty_url_is_noop(self):
        self.assertEqual(acr.with_fallback_link("hi", ""), "hi")

    def test_labels(self):
        self.assertEqual(acr.nav_link_label("/eyeglasses/all-spectacle-frames.html?color=black"),
                         "Open recommended frames")
        self.assertEqual(acr.nav_link_label("/lenses"), "Open lens options")
        self.assertEqual(acr.nav_link_label("/checkout"), "Go to checkout")
        self.assertEqual(acr.nav_link_label("/catalog/item?pid=294"), "Open this frame")


class EventLoggingTests(unittest.TestCase):
    def test_log_event_never_raises(self):
        class BoomCursor:
            def execute(self, *a, **k):
                raise RuntimeError("db down")

        class BoomDB:
            def cursor(self):
                return BoomCursor()

        # best-effort logging must swallow errors so it never breaks a reply
        acr.log_event(BoomDB(), "AI_TEST_EVENT", session_id="s1")


if __name__ == "__main__":
    unittest.main()
