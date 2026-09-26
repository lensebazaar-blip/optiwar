import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reports import reship_report_section as rrs  # noqa: E402
from reports.report_severity import ACTION, WARNING  # noqa: E402


def _sql_with(answers):
    def sql(query):
        for needle, rows in answers:
            if needle in query:
                if isinstance(rows, Exception):
                    raise rows
                return rows
        return [("0",)]
    return sql


class ReshipReportSectionTests(unittest.TestCase):
    def test_metrics_and_alerts_are_rendered_from_the_rows(self):
        sql = _sql_with([
            ("status IN ('RETURNED','PAYMENT_PENDING','PAID')", [("4",)]),
            ("AND hold_reason IS NULL AND abandon_at IS NOT NULL AND abandon_at <= NOW() + INTERVAL 5 DAY ORDER BY",
             [("ORD-1", "2026-11-25 10:00:00")]),
            ("AND hold_reason IS NULL AND abandon_at IS NOT NULL", [("1",)]),
            ("status='PAID' AND paid_at <", [("ORD-2", "2026-09-20 09:00:00")]),
            ("ops_sync_status IS NULL OR ops_sync_status IN", [("ORD-3", "2026-09-25 00:10:00")]),
        ])
        metrics, alerts, errors = rrs.collect(sql)
        self.assertEqual(errors, [])
        self.assertEqual(metrics["held"], 4)
        self.assertEqual(metrics["approaching"], 1)
        text = rrs.build(metrics, alerts, errors)
        self.assertIn("Returned parcels held", text)
        self.assertIn("Awaiting Rs 250 payment", text)
        self.assertIn("Paid - awaiting reship", text)
        self.assertIn("Approaching abandonment (<= 5 days)", text)
        self.assertIn("Abandoned (last 24h)", text)
        self.assertIn("Reshipped (last 24h)", text)
        self.assertIn("STATUS: RED", text)  # an unsynced abandonment is the worst case
        self.assertIn("ORD-1", text)
        self.assertIn("ORD-2", text)
        self.assertIn("ORD-3", text)
        sev = {f.message.split(":")[0]: f.severity for f in rrs.findings(metrics, alerts, errors)}
        self.assertEqual(sev["ABANDONED but the Ops platform has not acknowledged it"], ACTION)
        self.assertEqual(sev["Inside the final 5 days before abandonment"], WARNING)
        self.assertEqual(sev["RESHIP_PAID for more than 48h and not shipped"], WARNING)

    def test_green_when_nothing_is_wrong(self):
        metrics, alerts, errors = rrs.collect(lambda q: [] if "SELECT order_id" in q
                                             or "SELECT r.order_id" in q else [("0",)])
        self.assertEqual(rrs.status_of(metrics, alerts), rrs.GREEN)
        self.assertNotIn("[ALERT]", rrs.build(metrics, alerts, errors))

    def test_db_failure_prints_na_not_zero(self):
        def broken(query):
            raise rrs.SqlError("access denied")
        metrics, alerts, errors = rrs.collect(broken)
        text = rrs.build(metrics, alerts, errors)
        self.assertTrue(all(v is None for v in metrics.values()))
        self.assertIn("n/a", text)
        self.assertIn("STATUS: AMBER", text)
        for _key, label, _q in rrs.METRICS:
            line = [ln for ln in text.splitlines() if label in ln][0]
            self.assertTrue(line.rstrip().endswith("n/a"), line)
        self.assertTrue(all(f.severity == WARNING for f in rrs.findings(metrics, alerts, errors)))


if __name__ == "__main__":
    unittest.main()
