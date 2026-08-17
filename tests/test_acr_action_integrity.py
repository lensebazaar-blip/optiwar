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


class CanaryGateTests(unittest.TestCase):
    def test_master_switch_off_disables_everyone(self):
        # actions disabled -> off regardless of cookie/email/canary_only
        self.assertFalse(acr.canary_allows(False, True, True, "a@b.com", "a@b.com"))
        self.assertFalse(acr.canary_allows(False, False, True, "a@b.com", "a@b.com"))

    def test_full_rollout_when_not_canary_only(self):
        # enabled + not canary_only -> on for everyone
        self.assertTrue(acr.canary_allows(True, False, False, "", ""))
        self.assertTrue(acr.canary_allows(True, False, False, "anyone@x.com", ""))

    def test_canary_only_requires_cookie_or_allowlisted_email(self):
        # enabled + canary_only, no cookie, not allow-listed -> off
        self.assertFalse(acr.canary_allows(True, True, False, "cust@x.com", "staff@x.com"))
        # valid canary cookie -> on
        self.assertTrue(acr.canary_allows(True, True, True, "cust@x.com", ""))
        # allow-listed email (case-insensitive, trims list) -> on
        self.assertTrue(acr.canary_allows(True, True, False, "Staff@X.com", " staff@x.com , qa@x.com "))
        self.assertTrue(acr.canary_allows(True, True, False, "qa@x.com", "staff@x.com,qa@x.com"))

    def test_canary_only_empty_email_and_list_is_off(self):
        self.assertFalse(acr.canary_allows(True, True, False, "", ""))
        self.assertFalse(acr.canary_allows(True, True, False, None, ""))


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


class SupersedingAConfirmationKeepsTheEvidenceTests(unittest.TestCase):
    """`SUPERSEDED` does not say why. Since the gateway confirms every navigation
    immediately, an action whose arrival never came is normally *replaced* by the
    customer's next request rather than swept — so without an event here the
    broken journey leaves no trace at all: the row is no longer CONFIRMED for QC
    to see, and no longer PENDING/CONFIRMED for the sweep to reach."""

    class _Cur(object):
        def __init__(self, stranded):
            self.stranded = list(stranded)
            self.sql = []
            self.params = []

        def execute(self, sql, params=None):
            self.sql.append(" ".join(sql.split()))
            self.params.append(params)
            self._rows = ([{"action_id": a} for a in self.stranded]
                          if sql.strip().upper().startswith("SELECT") else [])

        def fetchall(self):
            return getattr(self, "_rows", [])

        def fetchone(self):
            return None

    class _DB(object):
        def __init__(self, cur):
            self._cur = cur

        def cursor(self):
            return self._cur

    def _events(self, cur):
        out = []
        for sql, params in zip(cur.sql, cur.params):
            if sql.startswith("INSERT INTO ai_events"):
                out.append(params)
        return out

    def test_a_stranded_confirmation_that_is_replaced_is_recorded_as_such(self):
        cur = self._Cur(["old1"])
        new_id = acr.create_pending_action(self._DB(cur), "s1", "NAVIGATE", "/b")
        expiries = [p for p in self._events(cur)
                    if acr.EV_ACTION_EXPIRED in p
                    and "confirmed_never_executed" in p]
        self.assertEqual(len(expiries), 1)
        self.assertIn("old1", expiries[0])
        payload = [v for v in expiries[0] if isinstance(v, str) and v.startswith("{")][0]
        self.assertIn('"from_status": "CONFIRMED"', payload)
        self.assertIn('"reason": "superseded"', payload)
        # The replacement is named, so the two rows can be read as one journey.
        self.assertIn(new_id, payload)

    def test_the_evidence_is_written_before_the_replacement_row(self):
        # The supersede has already happened by then; if the insert of the new
        # action fails, the defect must still be on the record.
        cur = self._Cur(["old1"])
        acr.create_pending_action(self._DB(cur), "s1", "NAVIGATE", "/b")
        expiry_at = next(i for i, s in enumerate(cur.sql)
                         if s.startswith("INSERT INTO ai_events"))
        insert_at = next(i for i, s in enumerate(cur.sql)
                         if s.startswith("INSERT INTO ai_actions"))
        self.assertLess(expiry_at, insert_at)

    def test_a_confirmation_still_in_flight_is_not_accused(self):
        # Selected, not filtered in Python: an in-flight confirmation must never
        # reach the loop, and the window is the one acr_qc reads.
        cur = self._Cur([])
        acr.create_pending_action(self._DB(cur), "s1", "NAVIGATE", "/b")
        select = next(s for s in cur.sql if s.startswith("SELECT action_id"))
        self.assertIn("status='CONFIRMED'", select)
        self.assertIn("resolved_at < DATE_SUB(NOW(), INTERVAL %s SECOND)", select)
        params = cur.params[cur.sql.index(select)]
        self.assertIn(acr.EXECUTION_TTL_SECONDS, params)
        self.assertEqual([p for p in self._events(cur)
                          if acr.EV_ACTION_EXPIRED in p], [])

    def test_an_unanswered_offer_is_not_reported_as_a_broken_journey(self):
        # Superseding a PENDING row is ordinary conversation, not a defect: the
        # customer never said yes. Only the CONFIRMED select feeds the loop.
        cur = self._Cur([])
        acr.create_pending_action(self._DB(cur), "s1", "NAVIGATE", "/b")
        offered = [p for p in self._events(cur) if acr.EV_NAVIGATION_OFFERED in p]
        self.assertEqual(len(offered), 1)
        self.assertEqual([p for p in self._events(cur)
                          if acr.EV_ACTION_EXPIRED in p], [])


if __name__ == "__main__":
    unittest.main()
