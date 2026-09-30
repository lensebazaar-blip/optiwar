"""Tests for the ACR daily-report section (Part A).

These are DB-free: they verify the section renders and, critically, degrades
gracefully (never raises) when the reporting DB is unavailable, and that it
never emits fabricated data for not-yet-instrumented metrics.
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(__file__)
_PATH = os.path.join(_HERE, "..", "reports", "acr_report_section.py")
_spec = importlib.util.spec_from_file_location("acr_report_section", _PATH)
acr_report_section = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acr_report_section)


class TestAcrReportSection(unittest.TestCase):
    def setUp(self):
        # Ensure no DB config is present so run_sql fails closed.
        for k in ("ACR_REPORT_DB_HOST", "ACR_REPORT_DB_USER", "ACR_REPORT_DB_PASS",
                  "ACR_REPORT_DB_NAME", "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD",
                  "MYSQL_DB", "MYSQL_DATABASE"):
            os.environ.pop(k, None)

    def test_run_sql_without_credentials_raises_sqlerror(self):
        with self.assertRaises(acr_report_section.SqlError):
            acr_report_section.run_sql("SELECT 1")

    def test_build_never_raises_when_db_unavailable(self):
        out = acr_report_section.build()
        self.assertIsInstance(out, str)
        self.assertIn("ACR AI OPERATIONS", out)

    def test_not_instrumented_metrics_render_as_na_not_zero(self):
        out = acr_report_section.build()
        # Revenue/purchases are T3 (attribution) and must never be faked.
        self.assertIn("Revenue assisted", out)
        self.assertIn(acr_report_section.NA, out)

    def test_revenue_lines_render_and_never_mix_reship_fees(self):
        s = acr_report_section
        out = s.build()
        self.assertIn("Revenue assisted", out)
        self.assertIn("RESHIPPING_REVENUE", out)
        self.assertIn("Added to cart", out)
        self.assertIn("Attribution basis", out)
        # every stage the report prints a line for is one funnel() keeps
        for stage in ("LISTING", "PRODUCT", "CART", "CHECKOUT", "PURCHASE"):
            self.assertIn(stage, s.FUNNEL_STAGES)
        self.assertEqual(s._fmt_money({}), "0")
        self.assertEqual(s._fmt_money({"EUR": {"orders": 2, "amount": 61.9},
                                       "INR": {"orders": 1, "amount": 2479}}),
                         "EUR 61.90 (2 orders) | INR 2479.00 (1 orders)")
        self.assertEqual(s._fmt_reship_revenue(
            {"INR": {"fees": 2, "amount": 500.0, "ai_assisted_orders": 1}}),
            "INR 500.00 (2 fees, 1 on AI-assisted orders)")
        self.assertEqual(s._fmt_money(s.NotEmitted("pending")), "pending")
        self.assertEqual(s._fmt_money(None), s.NA)

    def test_outcome_distribution_names_every_class_even_when_absent(self):
        # the first closure canary produced only ESCALATED; the report must not
        # crash on the classes the window has none of
        s = acr_report_section
        self.assertEqual(
            s._fmt_dist({"ESCALATED": 10}, ("ANSWERED", "ESCALATED", "ABANDONED", "FAILED")),
            "ANSWERED 0 | ESCALATED 10 | ABANDONED 0 | FAILED 0")

    def test_degraded_data_never_fabricates_green_all_clear(self):
        # With no DB, the core action query degrades. The report must NOT print a
        # false "Failures 0" / GREEN all-clear; it should flag data incomplete and
        # render the failure counter as n/a.
        out = acr_report_section.build()
        self.assertIn("(data incomplete)", out)
        self.assertNotIn("STATUS: GREEN", out)
        self.assertNotIn("Failures              0", out)

    def test_worst_status_ordering(self):
        s = acr_report_section
        self.assertEqual(s._worst(s.GREEN, s.AMBER, s.RED), s.RED)
        self.assertEqual(s._worst(s.GREEN, s.AMBER), s.AMBER)
        self.assertEqual(s._worst(s.GREEN, None), s.GREEN)
        self.assertEqual(s._worst(None), s.GREEN)

    def test_nav_execution_rate_excludes_non_terminal(self):
        s = acr_report_section
        # PENDING is in-flight, not a failure: 9 executed / 9 terminal = 100%.
        self.assertEqual(s._nav_execution_rate({"EXECUTED": 9, "PENDING": 5}), 100.0)
        # 3 executed of 4 terminal (1 FAILED) = 75%.
        self.assertEqual(s._nav_execution_rate({"EXECUTED": 3, "FAILED": 1, "PENDING": 2}), 75.0)
        # No terminal outcomes yet -> not meaningful.
        self.assertIsNone(s._nav_execution_rate({"PENDING": 4}))
        self.assertIsNone(s._nav_execution_rate({}))
        self.assertIsNone(s._nav_execution_rate(None))
        # Time-expired offers count as non-executed terminal outcomes: 3 executed
        # of (3 executed + 1 expired) = 75%, not a fabricated 100%.
        self.assertEqual(s._nav_execution_rate({"EXECUTED": 3, "PENDING": 2}, 1), 75.0)

    def test_rate_status_thresholds(self):
        s = acr_report_section
        self.assertEqual(s._rate_status(96, 95, 85), s.GREEN)
        self.assertEqual(s._rate_status(90, 95, 85), s.AMBER)
        self.assertEqual(s._rate_status(80, 95, 85), s.RED)
        self.assertIsNone(s._rate_status(None, 95, 85))


class TestInstrumentationCoverage(unittest.TestCase):
    """Coverage must be reported, and thin coverage must not read as GREEN."""

    def setUp(self):
        # Same fail-closed precondition as the suite above: no DB config, so
        # the section degrades and coverage is exercised at its thinnest.
        for k in ("ACR_REPORT_DB_HOST", "ACR_REPORT_DB_USER", "ACR_REPORT_DB_PASS",
                  "ACR_REPORT_DB_NAME", "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD",
                  "MYSQL_DB", "MYSQL_DATABASE"):
            os.environ.pop(k, None)
        acr_report_section._reset_cache()
        self.addCleanup(acr_report_section._reset_cache)

    def test_coverage_percentage(self):
        live = {str(i): 1 for i in range(13)}
        self.assertEqual(acr_report_section._coverage(live, 26),
                         (13, 26, 100.0 * 13 / 39))

    def test_coverage_of_empty_section_is_zero_not_a_crash(self):
        self.assertEqual(acr_report_section._coverage({}, 0), (0, 0, 0.0))

    def test_metrics_that_failed_do_not_count_as_live(self):
        self.assertEqual(acr_report_section._coverage({"a": None, "b": 1}, 0)[0], 1)

    def test_active_sessions_with_zero_started_is_a_contradiction(self):
        """24 open-status rows active inside the window, 0 sessions, 0
        conversations: the counters disagree."""
        msg = acr_report_section._telemetry_contradiction(
            {"sessions_open_status": {"total": 24, "stale": 0},
             "sessions_started": (0, 0, 0), "legacy_ai_started": 0})
        self.assertIsNotNone(msg)
        self.assertIn("24 open-status session(s)", msg)

    def test_stale_open_status_rows_are_not_a_contradiction(self):
        """Production: 127 status='active' rows, 120 of them last active before
        the window. Those are a retention question, not a telemetry gap."""
        self.assertIsNone(acr_report_section._telemetry_contradiction(
            {"sessions_open_status": {"total": 120, "stale": 120},
             "sessions_started": (0, 0, 0), "legacy_ai_started": 0}))

    def test_genuinely_quiet_day_is_not_a_contradiction(self):
        self.assertIsNone(acr_report_section._telemetry_contradiction(
            {"sessions_open_status": {"total": 0, "stale": 0},
             "sessions_started": (0, 0, 0), "legacy_ai_started": 0}))

    def test_consistent_activity_is_not_a_contradiction(self):
        self.assertIsNone(acr_report_section._telemetry_contradiction(
            {"sessions_open_status": {"total": 5, "stale": 0},
             "sessions_started": (2, 1, 3), "legacy_ai_started": 4}))

    def test_open_status_is_labelled_all_time_not_active_sessions(self):
        out = acr_report_section.build()
        self.assertIn("ALL TIME", out)
        self.assertNotIn("active sessions", out)

    def test_every_ai_count_is_rendered_with_its_source(self):
        out = acr_report_section.build()
        self.assertIn("EVERY COUNT WITH ITS SOURCE", out)
        self.assertIn("ai_events.MODEL_CALL", out)
        self.assertIn("chat_events.ai_started", out)
        self.assertIn("ai_metrics.log", out)

    def test_cost_is_never_estimated_from_a_guessed_price(self):
        out = acr_report_section.build()
        self.assertIn(acr_report_section.NA_COST, out)
        self.assertNotIn("$", out)

    def test_never_emitted_event_is_pending_not_zero(self):
        s = acr_report_section
        v = s.NotEmitted(s.NA_LEDGER)
        self.assertFalse(s._is_live(v))
        self.assertEqual(s._coverage({"a": v, "b": 3}, 0)[0], 1)
        self.assertEqual(str(v), s.NA_LEDGER)

    def test_ledger_metrics_are_zero_once_the_closure_job_has_run(self):
        # A job that closed sessions but found no paid order to attribute has
        # run: an empty commerce ledger is then a true 0, not "not scheduled".
        s = acr_report_section
        orig = s._ever
        calls = []
        try:
            s._ever = lambda ev: calls.append(ev) or ev == s.EV_SESSION_OUTCOME
            self.assertEqual(s._ledger_gated(lambda: 0), 0)
            s._ever = lambda ev: False
            v = s._ledger_gated(lambda: 0)
            self.assertIsInstance(v, s.NotEmitted)
            self.assertEqual(str(v), s.NA_LEDGER)
        finally:
            s._ever = orig
        self.assertIn(s.EV_SESSION_OUTCOME, calls)

    def test_section_reports_its_coverage(self):
        out = acr_report_section.build()
        self.assertIn("DATA COVERAGE", out)
        self.assertIn("COVERAGE", out)

    def test_thin_coverage_is_never_green(self):
        """Many n/a rows must produce AMBER, not a confident all-clear."""
        out = acr_report_section.build()
        self.assertIn("instrumentation coverage incomplete", out)
        self.assertNotIn("STATUS: GREEN", out)

    def test_no_placeholder_leaks_into_the_rendered_section(self):
        out = acr_report_section.build()
        for token in ("{{STATUS}}", "{{BAR}}", "{{COVERAGE}}"):
            self.assertNotIn(token, out)

    def test_findings_reports_degraded_ledger_for_the_aggregator(self):
        found = acr_report_section.findings()
        self.assertTrue(found)
        self.assertTrue(any("action ledger unavailable" in f.message
                            for f in found))
        self.assertTrue(all(f.source == "acr" for f in found))


if __name__ == "__main__":
    unittest.main()


class TestMultilingualSection(unittest.TestCase):
    def setUp(self):
        for k in ("ACR_REPORT_DB_HOST", "ACR_REPORT_DB_USER", "ACR_REPORT_DB_PASS",
                  "ACR_REPORT_DB_NAME", "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD",
                  "MYSQL_DB", "MYSQL_DATABASE"):
            os.environ.pop(k, None)

    def test_language_lines_render_without_a_database(self):
        out = acr_report_section.build()
        for label in ("Languages (sessions by detected language", "low-language-confidence turns",
                      "language-related escalations", "avoidable (AI misunderstood)",
                      "top misunderstood intents"):
            self.assertIn(label, out)

    def test_buckets_are_the_reported_five(self):
        b = acr_report_section.language_bucket
        self.assertEqual([b("en"), b("hi"), b("hi-Latn"), b("ta"), b(None)],
                         ["English", "Hindi", "Hinglish", "Other Indian", "Unknown"])

    def test_the_queries_read_payload_fields_never_message_text(self):
        with open(_PATH, encoding="utf-8") as fh:
            src = fh.read()
        block = src[src.index("# ── multilingual understanding"):src.index("# ── funnel")]
        self.assertNotIn("chat_messages", block)
        self.assertNotIn("content", block)


class TestKetMappingAndLookupLines(unittest.TestCase):
    def setUp(self):
        for k in ("ACR_REPORT_DB_HOST", "ACR_REPORT_DB_USER", "ACR_REPORT_DB_PASS",
                  "ACR_REPORT_DB_NAME", "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD",
                  "MYSQL_DB", "MYSQL_DATABASE"):
            os.environ.pop(k, None)
        acr_report_section._COLLECT_CACHE[:] = []

    def tearDown(self):
        acr_report_section._COLLECT_CACHE[:] = []

    def _findings_with(self, **metrics):
        acr_report_section._COLLECT_CACHE[:] = [(dict(metrics), [])]
        return acr_report_section.findings()

    def test_a_ket_ticket_without_a_mapping_row_is_an_action(self):
        found = self._findings_with(nav_actions={}, ket_tickets_unmapped=2)
        msgs = [f.message for f in found if f.severity == "ACTION"]
        self.assertEqual(len(msgs), 1)
        self.assertIn("2 KET ticket(s) have no optiwar_ticket_mapping row", msgs[0])

    def test_every_ket_ticket_mapped_raises_nothing(self):
        found = self._findings_with(nav_actions={}, ket_tickets_unmapped=0)
        self.assertFalse([f for f in found if "optiwar_ticket_mapping" in f.message])

    def test_lookup_lines_render_without_a_database(self):
        out = acr_report_section.build()
        for label in ("found nothing on file", "prescription lookups",
                      "KET tickets without a mapping row"):
            self.assertIn(label, out)

    def test_a_lookup_that_found_nothing_is_not_a_tool_failure(self):
        with open(_PATH, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('safe("contact_tool_failed", _count(EV_TOOL_USED, '
                      '"AND success=0 AND failure_code=\'lookup_failed\'"))', src)
