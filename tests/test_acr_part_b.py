"""Tests for ACR Part-B canonical instrumentation primitives.

Pure, DB/Flask-free coverage of the new building blocks:

  - server-side navigation-safety policy (is_safe_nav_url);
  - event-safe URL sanitisation (query/fragment stripped);
  - log_event falls back to the legacy column set when the Part-B typed
    columns are absent, and never raises;
  - the ai_client telemetry seam records one entry per round-trip and pop_calls
    drains it (skipped if the wrapper's optional deps are unavailable).

    python3 -m unittest tests.test_acr_part_b
"""
import importlib.util
import json
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_acr():
    spec = importlib.util.spec_from_file_location(
        "acr_under_test_b", os.path.join(REPO, "acr.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


acr = _load_acr()


class SafeNavUrlTests(unittest.TestCase):
    def test_relative_paths_are_safe(self):
        for u in ["/eyeglasses/all-spectacle-frames.html",
                  "/catalog/item?pid=294", "/"]:
            self.assertTrue(acr.is_safe_nav_url(u), u)

    def test_optiwar_hosts_are_safe(self):
        for u in ["https://optiwar.com/x", "https://in.optiwar.com/y",
                  "http://optiwar.in/z", "https://www.optiwar.com/"]:
            self.assertTrue(acr.is_safe_nav_url(u), u)

    def test_offsite_and_dangerous_schemes_are_blocked(self):
        for u in ["https://evil.com/x", "//evil.com/x",
                  "javascript:alert(1)", "data:text/html,x",
                  "http://optiwar.com.evil.com/"]:
            self.assertFalse(acr.is_safe_nav_url(u), u)

    def test_empty_is_not_gated(self):
        # No navigation to gate -> treated as safe (nothing is blocked).
        self.assertTrue(acr.is_safe_nav_url(None))
        self.assertTrue(acr.is_safe_nav_url(""))


class SanitizeUrlTests(unittest.TestCase):
    def test_query_and_fragment_stripped(self):
        self.assertEqual(
            acr.sanitize_url_for_event("https://optiwar.com/a?email=x@y.com#f"),
            "https://optiwar.com/a")

    def test_relative_path_query_stripped(self):
        self.assertEqual(
            acr.sanitize_url_for_event("/catalog?token=secret"), "/catalog")

    def test_none(self):
        self.assertIsNone(acr.sanitize_url_for_event(None))


class LogEventColumnFallbackTests(unittest.TestCase):
    class _Cursor:
        def __init__(self, fail_wide):
            self.fail_wide = fail_wide
            self.statements = []

        def execute(self, sql, params=None):
            wide = "consent_scope" in sql
            if wide and self.fail_wide:
                raise RuntimeError("Unknown column 'consent_scope'")
            self.statements.append(sql)

    class _DB:
        def __init__(self, fail_wide):
            self._cur = LogEventColumnFallbackTests._Cursor(fail_wide)

        def cursor(self):
            return self._cur

    def test_uses_wide_insert_when_columns_exist(self):
        db = self._DB(fail_wide=False)
        acr.log_event(db, acr.EV_MODEL_CALL, session_id="s", provider="deepseek",
                      model="deepseek-chat", workload="deepseek_chat",
                      request_id="rid", consent_scope=acr.CONSENT_FUNCTIONAL)
        self.assertEqual(len(db._cur.statements), 1)
        self.assertIn("consent_scope", db._cur.statements[0])

    def test_falls_back_to_legacy_insert_when_columns_missing(self):
        db = self._DB(fail_wide=True)
        acr.log_event(db, acr.EV_SESSION_STARTED, session_id="s",
                      consent_scope=acr.CONSENT_FUNCTIONAL)
        # exactly one legacy insert recorded (the wide one raised, was retried)
        self.assertEqual(len(db._cur.statements), 1)
        self.assertNotIn("consent_scope", db._cur.statements[0])

    def test_never_raises_when_both_fail(self):
        class BoomCursor:
            def execute(self, *a, **k):
                raise RuntimeError("db down")

        class BoomDB:
            def cursor(self):
                return BoomCursor()

        acr.log_event(BoomDB(), acr.EV_MODEL_TIMEOUT, session_id="s")


class FunnelStageTests(unittest.TestCase):
    """The commerce funnel stage is read off the page path only."""

    def test_product_checkout_and_success_pages(self):
        self.assertEqual(acr.funnel_stage_for_url(
            "https://optiwar.com/categories/eyeglasses/am29-frame"), acr.STAGE_PRODUCT)
        self.assertEqual(acr.funnel_stage_for_url(
            "https://optiwar.com/lenses/precision1/"), acr.STAGE_PRODUCT)
        self.assertEqual(acr.funnel_stage_for_url(
            "https://optiwar.com/checkout?step=2"), acr.STAGE_CHECKOUT)
        self.assertEqual(acr.funnel_stage_for_url(
            "https://in.optiwar.com/success/ORD1?token=x"), acr.STAGE_PURCHASE)

    def test_listing_pages(self):
        for u in ("https://optiwar.com/eyeglasses", "https://optiwar.com/categories/",
                  "https://optiwar.com/search?q=round", "https://optiwar.com/lenses"):
            self.assertEqual(acr.funnel_stage_for_url(u), acr.STAGE_LISTING, u)

    def test_pages_outside_the_funnel_have_no_stage(self):
        for u in ("", None, "https://optiwar.com/", "https://optiwar.com/profile/?tab=faces",
                  "https://optiwar.com/support", "not a url"):
            self.assertIsNone(acr.funnel_stage_for_url(u), u)

    def test_query_string_never_decides_the_stage(self):
        self.assertIsNone(acr.funnel_stage_for_url(
            "https://optiwar.com/profile/?next=/checkout"))

    def test_new_events_are_in_the_vocabulary(self):
        self.assertEqual(acr.EV_SESSION_RESUMED, "SESSION_RESUMED")
        self.assertEqual(acr.EV_SESSION_NOT_FOUND, "SESSION_NOT_FOUND")
        self.assertEqual(acr.EV_JOURNEY_STAGE, "JOURNEY_STAGE")
        self.assertEqual(acr.FUNNEL_STAGES,
                         ("LISTING", "PRODUCT", "CART", "CHECKOUT", "PURCHASE"))

    def test_cart_is_never_read_off_a_url(self):
        # CART is an act the add-to-cart routes record; no page maps to it.
        for u in ("https://optiwar.com/add_to_cart", "https://optiwar.com/cart",
                  "https://optiwar.com/checkout"):
            self.assertNotEqual(acr.funnel_stage_for_url(u), acr.STAGE_CART, u)


class _RecordingCursor:
    def __init__(self, last=None):
        self.statements = []
        self._last = last

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchone(self):
        return self._last

    def close(self):
        pass


class _RecordingDB:
    def __init__(self, last=None):
        self._cur = _RecordingCursor(last)
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def inserts(self):
        return [s for s in self._cur.statements if "INSERT INTO ai_events" in s[0]]


class BrowserStageTests(unittest.TestCase):
    """A commerce route records the browser's step against the chat session
    its signed cookie names — and against nothing else."""

    SECRET = "unit-test-secret"

    def _cookie(self, sid, secret=None):
        return {acr.CHAT_COOKIE_NAME:
                acr.chat_cookie_serializer(secret or self.SECRET).dumps(sid)}

    def test_a_valid_cookie_names_its_session(self):
        self.assertEqual(acr.session_from_chat_cookie(self._cookie("s1"), self.SECRET), "s1")

    def test_a_forged_or_missing_cookie_names_nobody(self):
        self.assertIsNone(acr.session_from_chat_cookie({}, self.SECRET))
        self.assertIsNone(acr.session_from_chat_cookie(
            {acr.CHAT_COOKIE_NAME: "s1.forged"}, self.SECRET))
        self.assertIsNone(acr.session_from_chat_cookie(
            self._cookie("s1", secret="another-secret"), self.SECRET))
        self.assertIsNone(acr.session_from_chat_cookie(self._cookie("s1"), ""))

    def test_no_cookie_records_nothing(self):
        db = _RecordingDB()
        self.assertIsNone(acr.log_browser_stage(db, {}, self.SECRET, acr.STAGE_CART))
        self.assertEqual(db.inserts(), [])
        self.assertEqual(db.commits, 0)

    def test_a_bound_browser_records_the_stage_and_commits(self):
        db = _RecordingDB()
        eid = acr.log_browser_stage(db, self._cookie("s1"), self.SECRET, acr.STAGE_CART,
                                    page_url="https://optiwar.com/add_to_cart?x=1")
        self.assertTrue(eid)
        (sql, params), = db.inserts()
        self.assertEqual(params[1], acr.EV_JOURNEY_STAGE)
        self.assertEqual(params[2], "s1")
        self.assertEqual(params[4], acr.STAGE_CART)
        self.assertNotIn("x=1", params[6])
        self.assertEqual(db.commits, 1)

    def test_purchase_carries_the_order_and_only_the_order(self):
        db = _RecordingDB()
        acr.log_browser_stage(db, self._cookie("s1"), self.SECRET, acr.STAGE_PURCHASE,
                              page_url="https://optiwar.com/success/ORD9?token=t",
                              order_id="ORD9")
        (sql, params), = db.inserts()
        self.assertEqual(params[4], acr.STAGE_PURCHASE)
        self.assertEqual(json.loads(params[10]), {"order_id": "ORD9"})
        self.assertNotIn("token", params[6])

    def test_the_same_step_directly_after_itself_is_one_step(self):
        last = {'journey_stage': acr.STAGE_PURCHASE, 'page_url': '/success/ORD9',
                'payload': json.dumps({"order_id": "ORD9"})}
        db = _RecordingDB(last=last)
        self.assertIsNone(acr.log_journey_stage(db, "s1", acr.STAGE_PURCHASE,
                                                page_url="/success/ORD9", order_id="ORD9"))
        self.assertEqual(db.inserts(), [])
        # a different order on the same stage is a new step
        self.assertTrue(acr.log_journey_stage(db, "s1", acr.STAGE_PURCHASE,
                                              page_url="/success/ORD10", order_id="ORD10"))

    def test_an_unknown_stage_is_refused(self):
        db = _RecordingDB()
        self.assertIsNone(acr.log_journey_stage(db, "s1", "PAYMENT"))
        self.assertEqual(db.inserts(), [])


class VocabularyTests(unittest.TestCase):
    def test_all_nineteen_event_types_present(self):
        expected = {
            "SESSION_STARTED", "RECOMMENDATION_GENERATED", "NAVIGATION_OFFERED",
            "ACTION_CONFIRMED", "ACTION_EXECUTED", "ACTION_FAILED",
            "ACTION_BLOCKED", "ACTION_EXPIRED", "PROMISE_WITHOUT_ACTION",
            "UNSAFE_URL_REJECTED", "MODEL_CALL", "MODEL_TIMEOUT",
            "ADMISSION_503", "PROVIDER_FAILURE", "HANDOVER_ESCALATED",
            "KET_TICKET_CREATED", "SESSION_OUTCOME", "OPS_CONSOLE_ACCESS",
            "OPS_CONSOLE_AUTH_FAILURE",
        }
        got = {getattr(acr, n) for n in dir(acr) if n.startswith("EV_")
               and n != "EV_SESSION_RESUMED"}
        self.assertTrue(expected.issubset(got))


class TelemetrySeamTests(unittest.TestCase):
    def test_record_and_pop(self):
        try:
            import ai_client  # noqa: F401
        except Exception:
            self.skipTest("ai_client optional deps unavailable")
        import ai_client
        ai_client.pop_calls()  # clear
        ai_client._record_call(kind="model_call", provider="deepseek",
                               input_tokens=None, output_tokens=None)
        ai_client._record_call(kind="model_timeout", provider="deepseek",
                               failure_code="provider_timeout")
        calls = ai_client.pop_calls()
        self.assertEqual([c["kind"] for c in calls],
                         ["model_call", "model_timeout"])
        self.assertEqual(ai_client.pop_calls(), [])

    def test_int_or_none(self):
        try:
            import ai_client
        except Exception:
            self.skipTest("ai_client optional deps unavailable")
        self.assertIsNone(ai_client._int_or_none(None))
        self.assertEqual(ai_client._int_or_none("5"), 5)
        self.assertIsNone(ai_client._int_or_none("x"))


if __name__ == "__main__":
    unittest.main()
