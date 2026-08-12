"""End-to-end regression for the false-green architecture.

Reconstructs the 2026-08-11 06:00 report — which printed ``ACTION=0`` and
``no CRITICAL or ACTION-REQUIRED findings`` above a sold-out review action, a
GMC disapproval and landing-page/image errors — and asserts the full pipeline
now refuses to call it healthy.

    python3 -m unittest tests.test_report_truth_gate
"""
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from reports import base_findings as bf  # noqa: E402
from reports import gmc_findings as gf  # noqa: E402
from reports import observatory_findings as of  # noqa: E402
from reports import report_executive as rx  # noqa: E402
from reports.report_severity import ACTION, CRITICAL, emit  # noqa: E402
from tests.test_gmc_findings import production_rows  # noqa: E402

# Structure of the real report: base sections first, GMC and ACR appended by
# the shell orchestrator *after* the base report has already been written.
BASE_REPORT = """======================================================================
  OPTIWAR DAILY REPORT
  Date: 2026-08-11  Time: 06:00:01
======================================================================
{{EXECUTIVE_STATUS}}

======================================================================
  SECTION 8: SALES RECONCILIATION
======================================================================
  8.7 SOLD-OUT >14 DAYS REVIEW LIST
      *** ACTION: 1 product(s) sold-out >14d - review restock vs discontinue ***

  --- RECONCILIATION SUMMARY ---
      * ACTION: 1 product(s) for discontinuation review
"""

GMC_SECTION = """
======================================================================
  SECTION D15: GOOGLE MERCHANT CENTER
======================================================================
"""

OBSERVATORY_TEXT = """
    [WARNING] (headers) optiwar.com: missing x-content-type-options response header
    [WARNING] (storefront) optiwar.in/x.html: 2 JSON-LD block(s) reference optiwar.com
"""


class TruthGateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.obs = os.path.join(self.dir, "seo_observatory_latest.txt")
        with open(self.obs, "w") as fh:
            fh.write(OBSERVATORY_TEXT)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run_pipeline(self, gmc_rows=None, include_acr=True):
        """Contributors emit, then the finalizer renders — the real order."""
        emit("base", bf.from_flags(
            ["ACTION: 1 product(s) for discontinuation review"]),
            sidecar_dir=self.dir)
        emit("observatory", of.findings(self.obs), sidecar_dir=self.dir)
        rows = production_rows() if gmc_rows is None else gmc_rows
        emit("gmc", gf.findings_from_summary(gf.summarize_product_views(rows)),
             sidecar_dir=self.dir)
        if include_acr:
            emit("acr", [], sidecar_dir=self.dir)
        report = BASE_REPORT + GMC_SECTION
        return rx.finalize(report, sidecar_dir=self.dir)

    def test_report_with_blocking_findings_is_never_all_clear(self):
        out = self._run_pipeline()
        self.assertNotIn("no CRITICAL or ACTION-REQUIRED findings", out)
        self.assertIn("require attention", out)

    def test_sold_out_action_reaches_the_executive_summary(self):
        out = self._run_pipeline()
        head = out.split("SECTION 8")[0]
        self.assertIn("discontinuation review", head)

    def test_gmc_disapproval_reaches_the_executive_summary(self):
        out = self._run_pipeline()
        head = out.split("SECTION 8")[0]
        self.assertIn("not eligible or disapproved", head)

    def test_gmc_reports_the_true_split_not_the_rollup_artefact(self):
        """The v2.1 rollup said 710/710; the authoritative view says 2/710."""
        out = self._run_pipeline()
        self.assertIn("2/710", out)
        self.assertNotIn("710/710", out)

    def test_observatory_warnings_are_counted_not_reprinted_as_the_verdict(self):
        out = self._run_pipeline()
        self.assertIn("x-content-type-options", out)
        self.assertIn("WARNING=", out)

    def test_a_section_that_fails_to_run_is_a_coverage_gap(self):
        out = self._run_pipeline(include_acr=False)
        self.assertIn("acr did not report", out)

    def test_clean_run_is_allowed_to_be_green(self):
        """The gate must not be a permanent red light."""
        emit("base", [], sidecar_dir=self.dir)
        emit("observatory", [], sidecar_dir=self.dir)
        emit("gmc", gf.findings_from_summary(gf.summarize_product_views(
            [{"offerId": "A", "aggregatedReportingContextStatus": "ELIGIBLE"}])),
            sidecar_dir=self.dir)
        emit("acr", [], sidecar_dir=self.dir)
        out = rx.finalize("HEADER\n{{EXECUTIVE_STATUS}}\n", sidecar_dir=self.dir)
        self.assertIn("no CRITICAL or ACTION-REQUIRED findings", out)

    def test_invariant_holds_for_every_severity_source(self):
        for label, findings in (
            ("base", bf.from_flags(["CRITICAL: negative inventory (3)"])),
            ("gmc", gf.findings_from_summary(gf.summarize_product_views(
                [{"offerId": "A",
                  "aggregatedReportingContextStatus":
                      "NOT_ELIGIBLE_OR_DISAPPROVED"}]))),
        ):
            d = tempfile.mkdtemp()
            emit(label, findings, sidecar_dir=d)
            out = rx.finalize("{{EXECUTIVE_STATUS}}\n", sidecar_dir=d)
            self.assertNotIn("no CRITICAL or ACTION-REQUIRED findings", out,
                             "%s findings did not break the all-clear" % label)
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
