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
