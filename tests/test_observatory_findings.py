"""Tests for Observatory -> structured severity parsing.

    python3 -m unittest tests.test_observatory_findings
"""
import os
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from reports import observatory_findings as of  # noqa: E402
from reports.report_severity import ACTION, CRITICAL, WARNING  # noqa: E402

SAMPLE = """OPTIWAR OPERATIONAL OBSERVATORY
  CRITICAL=0  ACTION=0  WARNING=2  INFO=8
  status: no CRITICAL or ACTION-REQUIRED findings
  --- WARNINGS ---
    [WARNING] (headers) optiwar.com: missing x-content-type-options response header
    [WARNING] (storefront) optiwar.in/x.html: 2 JSON-LD block(s) reference optiwar.com
"""


class ParseTests(unittest.TestCase):
    def test_severity_tagged_lines_become_findings(self):
        found = of.parse(SAMPLE)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0].severity, WARNING)
        self.assertEqual(found[0].category, "headers")
        self.assertIn("x-content-type-options", found[0].message)
        self.assertEqual(found[0].source, "observatory")

    def test_counts_line_is_not_mistaken_for_a_finding(self):
        self.assertEqual(of.parse("  CRITICAL=0  ACTION=0\n"), [])

    def test_line_without_category_still_parses(self):
        found = of.parse("  [CRITICAL] database unreachable\n")
        self.assertEqual(found[0].severity, CRITICAL)
        self.assertEqual(found[0].message, "database unreachable")

    def test_prose_is_ignored(self):
        self.assertEqual(of.parse("no action required\nall good\n"), [])


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.write(SAMPLE)

    def tearDown(self):
        os.unlink(self.path)

    def test_fresh_file_adds_no_staleness_finding(self):
        found = of.findings(self.path)
        self.assertFalse([f for f in found if f.category == "coverage"])

    def test_stale_file_is_an_action_not_a_pass(self):
        found = of.findings(self.path, now=time.time() + 40 * 3600)
        stale = [f for f in found if f.category == "coverage"]
        self.assertEqual(stale[0].severity, ACTION)
        self.assertIn("stale", stale[0].message)

    def test_missing_file_is_unknown_not_healthy(self):
        found = of.findings(self.path + ".nope")
        self.assertEqual(found[0].severity, ACTION)
        self.assertIn("UNKNOWN", found[0].message)


if __name__ == "__main__":
    unittest.main()
