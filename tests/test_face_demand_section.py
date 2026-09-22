"""Face Demand analytics count spectacle frames only.

Production had 32 not-matched views of CL-PRECISION1 (a contact lens:
vertical CONTACT_LENS, category NULL) in the frame-fit report. The write-time
rule is face_cart.is_frame_line (tested in test_face_cart); this is the same
predicate in SQL at report time. Historical rows stay in the log and are
excluded on read.
"""
import importlib.util
import os
import unittest


_HERE = os.path.dirname(__file__)
_PATH = os.path.join(_HERE, "..", "reports", "face_demand_section.py")
_spec = importlib.util.spec_from_file_location("face_demand_section", _PATH)
fds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fds)

class TestReportTimeFilter(unittest.TestCase):
    """Every demand query joins the product and applies the frame predicate."""

    def setUp(self):
        self.queries = []
        self._orig = fds.run_sql

        def fake(sql):
            self.queries.append(" ".join(sql.split()))
            return []
        fds.run_sql = fake
        self.addCleanup(setattr, fds, "run_sql", self._orig)

    def test_every_demand_log_query_is_frame_filtered(self):
        fds.build()
        demand = [q for q in self.queries if "face_demand_log" in q]
        self.assertGreaterEqual(len(demand), 5)
        for q in demand:
            if "Excluded" in q or "p.product_id IS NULL" in q:
                continue  # the disclosure row counts what the filter drops
            self.assertIn("product_category,'')) = 'Spectacles Frame'", q, q)
            self.assertIn("product_vertical,'') <> 'CONTACT_LENS'", q, q)
            self.assertIn("JOIN products p ON p.product_id = d.product_id", q, q)

    def test_stock_side_of_gap_analysis_counts_frames_only(self):
        fds.build()
        gap = [q for q in self.queries if "product_quantity > 0" in q]
        self.assertEqual(len(gap), 1)
        self.assertIn("product_quantity > 0 AND TRIM(COALESCE(product_category,''))"
                      " = 'Spectacles Frame'", gap[0])

    def test_exclusion_is_disclosed_not_silent(self):
        def fake(sql):
            if "p.product_id IS NULL" in sql:
                return [(32, 1)]
            return []
        fds.run_sql = fake
        out = fds.build()
        self.assertIn("Excluded from frame analytics: 32 log row(s) across 1", out)
        self.assertIn("Kept in face_demand_log", out)

    def test_headings_say_frames(self):
        out = fds.build()
        self.assertIn("spectacle frames only", out)
        self.assertNotIn("PRODUCT PAGE VIEWS", out)

    def test_never_deletes(self):
        fds.build()
        for q in self.queries:
            self.assertTrue(q.upper().startswith("SELECT"), q)


class TestNeverRaises(unittest.TestCase):
    def setUp(self):
        for k in ("ACR_REPORT_DB_HOST", "ACR_REPORT_DB_USER", "ACR_REPORT_DB_PASS",
                  "ACR_REPORT_DB_NAME", "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD",
                  "MYSQL_DB", "MYSQL_DATABASE"):
            os.environ.pop(k, None)

    def test_build_degrades_without_db(self):
        out = fds.build()
        self.assertIn("FACE DEMAND INTELLIGENCE", out)
        self.assertIn("[degraded]", out)


if __name__ == "__main__":
    unittest.main()
