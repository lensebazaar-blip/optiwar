import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reports import restock_report_section as rrs  # noqa: E402
from report_db import SqlError  # noqa: E402
from reports.report_severity import WARNING  # noqa: E402


class RestockReportSectionTests(unittest.TestCase):
    def test_rows_render_amber_and_create_one_warning_per_line(self):
        rows = [
            ("ABC-123", "AK79", "1", "2026-08-26", "87"),
            ("XYZ-456", "FR22", "1", "2026-08-26", "87"),
        ]

        text = rrs.build(rows)

        self.assertIn(
            "REFUNDS AWAITING RESTOCK (unshipped full refunds)   STATUS: AMBER",
            text)
        self.assertIn("OW-ABC-123", text)
        self.assertIn("OW-XYZ-456", text)
        self.assertIn("AK79 x1", text)
        self.assertIn("FR22 x1", text)
        self.assertIn("refunded 2026-08-26", text)
        self.assertIn("open 87d", text)
        self.assertIn(
            "Restock with: scripts/restock_refunded.py --by NAME --order <id> "
            "--apply (dry run first without --apply)", text)

        findings = rrs.findings(rows)
        self.assertEqual(len(findings), 2)
        self.assertEqual(
            [(f.severity, f.category, f.source, f.message) for f in findings],
            [
                (WARNING, "restock", "restock",
                 "Refund awaiting restock: OW-ABC-123 AK79 x1 "
                 "(refunded 2026-08-26, open 87d)"),
                (WARNING, "restock", "restock",
                 "Refund awaiting restock: OW-XYZ-456 FR22 x1 "
                 "(refunded 2026-08-26, open 87d)"),
            ])

    def test_no_rows_is_green(self):
        text = rrs.build([])

        self.assertIn(
            "REFUNDS AWAITING RESTOCK (unshipped full refunds)   STATUS: GREEN",
            text)
        self.assertIn(
            "None - every unshipped full refund has its stock back.", text)
        self.assertEqual(rrs.findings([]), [])

    def test_sql_error_is_amber_and_emits_warning(self):
        def broken(_query):
            raise SqlError("access denied")

        rows, error = rrs.collect(broken)

        text = rrs.build(rows, error)
        findings = rrs.findings(rows, error)

        self.assertIn(
            "REFUNDS AWAITING RESTOCK (unshipped full refunds)   STATUS: AMBER",
            text)
        self.assertIn(
            "[n/a] Refunds awaiting restock: not readable (access denied)", text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, WARNING)
        self.assertEqual(findings[0].message, "restock report: access denied")

        def empty_error(_query):
            raise SqlError()

        rows, error = rrs.collect(empty_error)
        self.assertIn("STATUS: AMBER", rrs.build(rows, error))
        self.assertEqual(error, "unknown SQL error")

    def test_query_guards_eligible_unshipped_lines_not_already_restocked(self):
        query = rrs.QUERY

        self.assertNotIn("LIMIT", query.upper())
        self.assertIn("stock_restocks", query)
        self.assertIn("ops_shipping_awb", query)
        self.assertIn("SMART_EYEWEAR", query)
        self.assertIn("refund_type = 'FULL'", query)
        self.assertIn("status = 'PROCESSED'", query)


if __name__ == "__main__":
    unittest.main()
