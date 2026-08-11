"""Tests for the Daily Report executive finalizer.

The property under test is the one today's report violated: the top-of-report
verdict must reflect sections appended *after* the base report was written.

    python3 -m unittest tests.test_report_executive
"""
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from reports import report_executive as rx  # noqa: E402
from reports.report_severity import (  # noqa: E402
    ACTION, CRITICAL, WARNING, Finding, emit,
)

BASE_REPORT = """OPTIWAR DAILY REPORT
{{EXECUTIVE_STATUS}}

SECTION 1: ERRORS
  nothing to report
"""


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_all_clear_only_when_nothing_blocking(self):
        emit("gmc", [Finding(WARNING, "gmc", "minor", "gmc")], sidecar_dir=self.dir)
        out = rx.finalize(BASE_REPORT, sidecar_dir=self.dir)
        self.assertIn("no CRITICAL or ACTION-REQUIRED findings", out)
        self.assertNotIn(rx.PLACEHOLDER, out)

    def test_section_appended_after_the_base_report_breaks_the_green(self):
        """The exact failure mode: GMC runs after the banner used to be written."""
        emit("gmc", [Finding(CRITICAL, "gmc", "710/710 products disapproved",
                             "gmc")], sidecar_dir=self.dir)
        out = rx.finalize(BASE_REPORT, sidecar_dir=self.dir)
        self.assertNotIn("no CRITICAL or ACTION-REQUIRED findings", out)
        self.assertIn("710/710 products disapproved", out)
        self.assertIn("CRITICAL=1", out)

    def test_banner_is_rendered_at_the_placeholder_position(self):
        emit("acr", [], sidecar_dir=self.dir)
        out = rx.finalize(BASE_REPORT, sidecar_dir=self.dir)
        lines = out.splitlines()
        self.assertEqual(lines[0], "OPTIWAR DAILY REPORT")
        self.assertTrue(lines[1].startswith("OPERATIONAL STATUS"))

    def test_missing_placeholder_still_gets_a_banner(self):
        out = rx.finalize("SECTION 1: ERRORS\n", sidecar_dir=self.dir)
        self.assertTrue(out.startswith("OPERATIONAL STATUS"))

    def test_silent_section_is_a_coverage_warning_not_a_pass(self):
        out = rx.finalize(BASE_REPORT, sidecar_dir=self.dir)
        for src in rx.EXPECTED_SOURCES:
            self.assertIn("%s did not report" % src, out)

    def test_reporting_section_with_no_findings_is_not_a_coverage_gap(self):
        for src in rx.EXPECTED_SOURCES:
            emit(src, [], sidecar_dir=self.dir)
        out = rx.finalize(BASE_REPORT, sidecar_dir=self.dir)
        self.assertNotIn("did not report", out)


class ScavengeTests(unittest.TestCase):
    """Un-migrated sections must not be able to hide a blocking finding."""

    def test_bracket_markers_are_recovered(self):
        found = rx.scavenge("  [ACTION] sold out 19 days\n  [CRITICAL] boom\n")
        sevs = [f[0] for f in found]
        self.assertIn(ACTION, sevs)
        self.assertIn(CRITICAL, sevs)

    def test_starred_warning_is_recovered_as_action(self):
        found = rx.scavenge("  *** WARNING: observatory data stale (>30h) ***\n")
        self.assertEqual(found[0][0], ACTION)
        self.assertIn("observatory data stale", found[0][2])

    def test_ordinary_prose_is_not_scavenged(self):
        text = ("  no action required today\n"
                "  critical thinking about warnings\n"
                "  status: healthy\n")
        self.assertEqual(rx.scavenge(text), [])

    def test_scavenged_action_breaks_a_green_banner(self):
        report = BASE_REPORT + "\n  [ACTION] product sold out for 19 days\n"
        out = rx.finalize(report, sidecar_dir=tempfile.mkdtemp())
        self.assertNotIn("no CRITICAL or ACTION-REQUIRED findings", out)
        self.assertIn("sold out for 19 days", out)

    def test_rerun_does_not_double_count_the_previous_banner(self):
        d = tempfile.mkdtemp()
        report = BASE_REPORT + "\n  [ACTION] sold out\n"
        once = rx.finalize(report, sidecar_dir=d)
        twice = rx.finalize(once, sidecar_dir=d)
        self.assertEqual(once.count("[ACTION] sold out"),
                         twice.count("[ACTION] sold out"))
        self.assertIn("ACTION=1", twice)


class DeduplicationTests(unittest.TestCase):
    """A migrated section prints what it also reports; count it once."""

    def test_a_reporting_section_is_not_scavenged_from_its_own_text(self):
        d = tempfile.mkdtemp()
        emit("gmc", [Finding(ACTION, "gmc", "2/710 products disapproved",
                             "gmc")], sidecar_dir=d)
        report = (BASE_REPORT +
                  "\n  SECTION D15: GOOGLE MERCHANT CENTER\n"
                  "  *** ACTION: 2 product(s) disapproved ***\n")
        out = rx.finalize(report, sidecar_dir=d)
        self.assertIn("ACTION=1", out)

    def test_a_silent_section_is_still_scavenged(self):
        """Region skipping must not become a way to hide a finding."""
        report = (BASE_REPORT +
                  "\n  SECTION D15: GOOGLE MERCHANT CENTER\n"
                  "  *** ACTION: 2 product(s) disapproved ***\n")
        out = rx.finalize(report, sidecar_dir=tempfile.mkdtemp())
        self.assertIn("ACTION=1", out)
        self.assertIn("2 product(s) disapproved", out)

    def test_same_message_from_two_paths_counts_once(self):
        d = tempfile.mkdtemp()
        emit("base", [Finding(ACTION, "operations",
                              "1 product(s) for discontinuation review",
                              "base")], sidecar_dir=d)
        # The base report reprints its own summary bullet outside its section.
        report = ("  * ACTION: 1 product(s) for discontinuation review\n"
                  + BASE_REPORT)
        out = rx.finalize(report, sidecar_dir=d)
        self.assertIn("ACTION=1", out)


class RollingLogTests(unittest.TestCase):
    def test_history_is_not_rewritten_when_todays_entry_is_missing(self):
        """The rolling log holds every past report; only today's may change."""
        history = BASE_REPORT.replace(
            rx.PLACEHOLDER, "OPERATIONAL STATUS\n  CRITICAL=0  ACTION=0\n")
        with self.assertRaises(rx.NoPlaceholder):
            rx.finalize(history, sidecar_dir=tempfile.mkdtemp(),
                        require_placeholder=True)


class InvariantTests(unittest.TestCase):
    def test_blocking_finding_never_coexists_with_all_clear(self):
        d = tempfile.mkdtemp()
        emit("gmc", [Finding(ACTION, "gmc", "landing page errors", "gmc")],
             sidecar_dir=d)
        out = rx.finalize(BASE_REPORT, sidecar_dir=d)
        self.assertNotIn("no CRITICAL or ACTION-REQUIRED findings", out)
        self.assertIn("1 finding(s) require attention", out)


if __name__ == "__main__":
    unittest.main()
