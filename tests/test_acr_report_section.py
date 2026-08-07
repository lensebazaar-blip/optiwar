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

    def test_worst_status_ordering(self):
        s = acr_report_section
        self.assertEqual(s._worst(s.GREEN, s.AMBER, s.RED), s.RED)
        self.assertEqual(s._worst(s.GREEN, s.AMBER), s.AMBER)
        self.assertEqual(s._worst(s.GREEN, None), s.GREEN)
        self.assertEqual(s._worst(None), s.GREEN)

    def test_nav_success_rate(self):
        s = acr_report_section
        self.assertEqual(s._nav_success_rate({"EXECUTED": 9, "PENDING": 1}), 90.0)
        self.assertIsNone(s._nav_success_rate({}))
        self.assertIsNone(s._nav_success_rate(None))

    def test_rate_status_thresholds(self):
        s = acr_report_section
        self.assertEqual(s._rate_status(96, 95, 85), s.GREEN)
        self.assertEqual(s._rate_status(90, 95, 85), s.AMBER)
        self.assertEqual(s._rate_status(80, 95, 85), s.RED)
        self.assertIsNone(s._rate_status(None, 95, 85))


if __name__ == "__main__":
    unittest.main()
