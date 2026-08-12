"""Tests for base-report action flags -> structured severity.

    python3 -m unittest tests.test_base_findings
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from reports import base_findings as bf  # noqa: E402
from reports.report_severity import ACTION, CRITICAL, WARNING  # noqa: E402

# Verbatim from optiwar_daily_report.py's reconciliation section.
PRODUCTION_FLAGS = [
    "CRITICAL: negative inventory (3)",
    "ACTION: 2 refund-pending line(s)",
    "WARNING: sales/fulfilled mismatch",
    "ACTION: 1 product(s) for discontinuation review",
]


class FromFlagsTests(unittest.TestCase):
    def test_severity_prefix_is_honoured(self):
        found = bf.from_flags(PRODUCTION_FLAGS)
        self.assertEqual([f.severity for f in found],
                         [CRITICAL, ACTION, WARNING, ACTION])
        self.assertEqual(found[0].message, "negative inventory (3)")
        self.assertEqual(found[0].source, "base")

    def test_sold_out_review_reaches_the_aggregator_as_action(self):
        """The finding today's report buried while claiming ACTION=0."""
        found = bf.from_flags(["ACTION: 1 product(s) for discontinuation review"])
        self.assertEqual(found[0].severity, ACTION)

    def test_unprefixed_flag_is_not_downgraded_to_info(self):
        found = bf.from_flags(["something odd happened"])
        self.assertEqual(found[0].severity, ACTION)

    def test_empty_and_blank_flags_are_dropped(self):
        self.assertEqual(bf.from_flags([]), [])
        self.assertEqual(bf.from_flags(["", "   "]), [])

    def test_category_is_attached(self):
        found = bf.from_flags(["ACTION: renew key"], category="deepseek")
        self.assertEqual(found[0].category, "deepseek")


if __name__ == "__main__":
    unittest.main()
