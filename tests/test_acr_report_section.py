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
        """Today's production report: 24 active, 0 sessions, 0 conversations."""
        msg = acr_report_section._telemetry_contradiction(
            {"sessions_active": 24, "sessions_started": (0, 0, 0),
             "conversations": 0})
        self.assertIsNotNone(msg)
        self.assertIn("24 active session(s)", msg)

    def test_genuinely_quiet_day_is_not_a_contradiction(self):
        self.assertIsNone(acr_report_section._telemetry_contradiction(
            {"sessions_active": 0, "sessions_started": (0, 0, 0),
             "conversations": 0}))

    def test_consistent_activity_is_not_a_contradiction(self):
        self.assertIsNone(acr_report_section._telemetry_contradiction(
            {"sessions_active": 5, "sessions_started": (2, 1, 3),
             "conversations": 4}))

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
