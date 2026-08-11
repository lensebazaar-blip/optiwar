"""Tests for GMC eligibility classification.

Fixtures mirror the real account payload observed on 2026-08-11: 710 products,
708 ELIGIBLE, 2 disapproved for image_link_internal_error across SHOPPING_ADS
and FREE_LISTINGS, 58 demoted for a missing attribute.

    python3 -m unittest tests.test_gmc_findings
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from reports import gmc_findings as gf  # noqa: E402
from reports.report_severity import ACTION, CRITICAL, INFO, WARNING  # noqa: E402

CONTEXTS = ("SHOPPING_ADS", "FREE_LISTINGS")


def _issue(code, severity, resolution="MERCHANT_ACTION", countries=("GB", "DE"),
           attribute=None):
    return {
        "type": {"code": code, "canonicalAttribute": attribute},
        "resolution": resolution,
        "severity": {
            "aggregatedSeverity": severity,
            "severityPerReportingContext": [
                {"reportingContext": ctx, "disapprovedCountries": list(countries)}
                for ctx in CONTEXTS
            ],
        },
    }


def _pv(offer, status, issues=()):
    return {"offerId": offer, "aggregatedReportingContextStatus": status,
            "itemIssues": list(issues)}


def production_rows():
    rows = [_pv("OK%03d" % i, "ELIGIBLE") for i in range(650)]
    rows += [_pv("DEM%03d" % i, "ELIGIBLE",
                 [_issue("missing_item_attribute_for_product_type", "DEMOTED",
                         attribute="color")])
             for i in range(58)]
    rows += [
        _pv("BD21", "NOT_ELIGIBLE_OR_DISAPPROVED",
            [_issue("image_link_internal_error", "DISAPPROVED")]),
        _pv("BH62", "NOT_ELIGIBLE_OR_DISAPPROVED",
            [_issue("missing_item_attribute_for_product_type", "DEMOTED"),
             _issue("image_link_internal_error", "DISAPPROVED")]),
    ]
    return rows


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.summary = gf.summarize_product_views(production_rows())

    def test_counts_match_the_authoritative_view(self):
        self.assertEqual(self.summary["total"], 710)
        self.assertEqual(self.summary["by_status"]["ELIGIBLE"], 708)
        self.assertEqual(
            self.summary["by_status"]["NOT_ELIGIBLE_OR_DISAPPROVED"], 2)

    def test_issue_is_counted_once_per_product_not_once_per_context(self):
        """'4 image_link_internal_error' was 2 products counted twice."""
        rec = self.summary["issues"][("image_link_internal_error", "DISAPPROVED")]
        self.assertEqual(len(rec["offers"]), 2)
        self.assertEqual(sorted(rec["contexts"]), ["FREE_LISTINGS", "SHOPPING_ADS"])

    def test_demotion_and_disapproval_are_tracked_separately(self):
        self.assertIn(("missing_item_attribute_for_product_type", "DEMOTED"),
                      self.summary["issues"])
        demoted = self.summary["issues"][
            ("missing_item_attribute_for_product_type", "DEMOTED")]
        self.assertEqual(len(demoted["offers"]), 59)

    def test_empty_input_is_not_an_error(self):
        s = gf.summarize_product_views([])
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["issues"], {})


class FindingsTests(unittest.TestCase):
    def _messages(self, rows):
        return [f.message for f in gf.findings_from_summary(
            gf.summarize_product_views(rows))]

    def test_two_bad_products_is_action_not_critical(self):
        findings = gf.findings_from_summary(
            gf.summarize_product_views(production_rows()))
        blocking = [f for f in findings if f.severity in (ACTION, CRITICAL)]
        self.assertTrue(blocking)
        self.assertFalse([f for f in blocking if f.severity == CRITICAL])
        self.assertIn("2/710", blocking[0].message)
        self.assertIn("product-specific", blocking[0].message)

    def test_catalogue_wide_disapproval_is_critical_and_systemic(self):
        rows = [_pv("P%03d" % i, "NOT_ELIGIBLE_OR_DISAPPROVED")
                for i in range(710)]
        findings = gf.findings_from_summary(gf.summarize_product_views(rows))
        top = findings[0]
        self.assertEqual(top.severity, CRITICAL)
        self.assertIn("systemic", top.message)

    def test_google_side_issue_is_labelled_as_not_merchant_work(self):
        msgs = self._messages(production_rows())
        img = [m for m in msgs if "image_link_internal_error" in m][0]
        self.assertIn("Google-side", img)

    def test_merchant_side_issue_is_labelled_merchant_action(self):
        msgs = self._messages(production_rows())
        attr = [m for m in msgs
                if "missing_item_attribute_for_product_type" in m][0]
        self.assertIn("merchant action", attr)

    def test_findings_name_the_reporting_contexts(self):
        msgs = self._messages(production_rows())
        img = [m for m in msgs if "image_link_internal_error" in m][0]
        self.assertIn("SHOPPING_ADS", img)
        self.assertIn("FREE_LISTINGS", img)

    def test_demotion_is_a_warning_not_an_action(self):
        findings = gf.findings_from_summary(
            gf.summarize_product_views(production_rows()))
        attr = [f for f in findings
                if "missing_item_attribute_for_product_type" in f.message][0]
        self.assertEqual(attr.severity, WARNING)

    def test_unaffected_issues_are_not_reported_as_work(self):
        rows = [_pv("A", "ELIGIBLE", [_issue("title_all_caps", "UNAFFECTED")])]
        msgs = self._messages(rows)
        self.assertFalse([m for m in msgs if "title_all_caps" in m])

    def test_no_data_is_unknown_not_healthy(self):
        findings = gf.findings_from_summary(gf.summarize_product_views([]))
        self.assertEqual(findings[0].severity, ACTION)
        self.assertIn("UNKNOWN", findings[0].message)

    def test_all_eligible_produces_no_blocking_finding(self):
        rows = [_pv("P%03d" % i, "ELIGIBLE") for i in range(10)]
        findings = gf.findings_from_summary(gf.summarize_product_views(rows))
        self.assertFalse([f for f in findings if f.severity in (ACTION, CRITICAL)])
        self.assertEqual(findings[-1].severity, INFO)


class DiagnosisTests(unittest.TestCase):
    def test_diagnosis_names_the_attribute_to_fix(self):
        text = gf.render_diagnosis(gf.summarize_product_views(production_rows()))
        self.assertIn("attr=color", text)

    def test_finding_names_the_attribute_to_fix(self):
        msgs = [f.message for f in gf.findings_from_summary(
            gf.summarize_product_views(production_rows()))]
        attr = [m for m in msgs
                if "missing_item_attribute_for_product_type" in m][0]
        self.assertIn("[color]", attr)

    def test_diagnosis_reports_scope_owner_and_context(self):
        text = gf.render_diagnosis(gf.summarize_product_views(production_rows()))
        self.assertIn("ELIGIBLE", text)
        self.assertIn("image_link_internal_error", text)
        self.assertIn("scope=product-specific", text)
        self.assertIn("owner=google", text)
        self.assertIn("SHOPPING_ADS", text)

    def test_diagnosis_without_data_says_unknown(self):
        text = gf.render_diagnosis(gf.summarize_product_views([]))
        self.assertIn("UNKNOWN", text)


if __name__ == "__main__":
    unittest.main()
