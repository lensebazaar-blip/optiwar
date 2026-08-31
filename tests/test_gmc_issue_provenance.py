"""GMC reporting: serving headline, offer provenance and day-over-day delta.

The account carried the same two issue groups every morning for weeks — 25
disapproved for a shipping/currency mismatch and 29 demoted for a missing
age_group — and the report could not say which of them were new, which had been
cleared, or whether the offers even came from our feed. These tests fix that
reading of the data:

  - the headline is stated in serving terms (submitted/eligible/disapproved/
    demoted/pending), and demoted is not folded into disapproved;
  - each issue names the feed label and currency of the affected offers, and an
    issue that touches no offer of ours says so, because no feed change fixes it;
  - the SKUs are listed, not only counted;
  - a second run reports what appeared and what cleared since the first.

    python3 -m unittest tests.test_gmc_issue_provenance
"""
import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from reports import gmc_findings as gf  # noqa: E402


def _issue(code, severity, attribute=None, countries=("DE",),
           resolution="MERCHANT_ACTION"):
    return {
        "type": {"code": code, "canonicalAttribute": attribute},
        "resolution": resolution,
        "severity": {
            "aggregatedSeverity": severity,
            "severityPerReportingContext": [
                {"reportingContext": "SHOPPING_ADS",
                 "disapprovedCountries": list(countries)},
            ],
        },
    }


def _pv(offer, status, issues=(), label="GLOBAL-EUR", currency="EUR"):
    return {"offerId": offer, "aggregatedReportingContextStatus": status,
            "feedLabel": label, "price": {"currencyCode": currency},
            "itemIssues": list(issues)}


def account_rows():
    """The 2026-08-31 shape: 702 clean, 29 demoted, 25 disapproved under IN."""
    rows = [_pv("OK%03d" % i, "ELIGIBLE") for i in range(677)]
    rows += [_pv("AG%02d" % i, "ELIGIBLE",
                 [_issue("missing_item_attribute_for_product_type", "DEMOTED",
                         attribute="age_group")])
             for i in range(29)]
    rows += [_pv("SHIP%02d" % i, "NOT_ELIGIBLE_OR_DISAPPROVED",
                 [_issue("missing_shipping_mismatch_of_shipping_method_"
                         "and_offer_currency", "DISAPPROVED",
                         attribute="shipping", countries=("IN",))],
                 label="IN")
             for i in range(25)]
    return rows


class HeadlineTests(unittest.TestCase):
    def setUp(self):
        self.summary = gf.summarize_product_views(account_rows())

    def test_headline_states_serving_status_not_just_totals(self):
        line = gf.headline(self.summary)
        self.assertIn("Submitted 731", line)
        self.assertIn("Eligible 706", line)
        self.assertIn("Disapproved 25", line)
        self.assertIn("Demoted 29", line)
        self.assertIn("Pending 0", line)

    def test_a_demoted_offer_is_still_eligible_and_counted_once(self):
        """Demoted offers serve, lower. Counting them as disapproved would
        overstate the outage by 29 products."""
        self.assertEqual(self.summary["demoted"], 29)
        self.assertEqual(self.summary["by_status"]["ELIGIBLE"], 706)

    def test_ownership_splits_our_work_from_google_s(self):
        merchant, google = gf.ownership(self.summary)
        self.assertEqual(merchant, 54)
        self.assertEqual(google, 0)

    def test_google_side_issue_is_not_counted_as_our_work(self):
        rows = [_pv("IMG1", "NOT_ELIGIBLE_OR_DISAPPROVED",
                    [_issue("image_link_internal_error", "DISAPPROVED")])]
        merchant, google = gf.ownership(gf.summarize_product_views(rows))
        self.assertEqual((merchant, google), (0, 1))


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.summary = gf.summarize_product_views(account_rows())

    def test_offer_origin_is_grouped_by_feed_label_and_currency(self):
        self.assertEqual(self.summary["by_source"][("GLOBAL-EUR", "EUR")], 706)
        self.assertEqual(self.summary["by_source"][("IN", "EUR")], 25)

    def test_issue_carries_the_feed_and_currency_of_its_offers(self):
        rec = self.summary["issues"][
            ("missing_shipping_mismatch_of_shipping_method_and_offer_currency",
             "DISAPPROVED")]
        self.assertEqual(sorted(rec["labels"]), ["IN"])
        self.assertEqual(sorted(rec["currencies"]), ["EUR"])

    def test_issue_outside_our_feed_says_no_feed_change_fixes_it(self):
        msgs = [f.message for f in gf.findings_from_summary(self.summary)]
        ship = [m for m in msgs if "mismatch_of_shipping_method" in m][0]
        self.assertIn("not from our GLOBAL-EUR feed", ship)
        self.assertIn("feed label IN", ship)
        self.assertIn("currency EUR", ship)

    def test_issue_inside_our_feed_is_not_blamed_on_something_else(self):
        msgs = [f.message for f in gf.findings_from_summary(self.summary)]
        age = [m for m in msgs if "missing_item_attribute" in m][0]
        self.assertNotIn("not from our", age)

    def test_diagnosis_lists_the_skus_to_work_on(self):
        text = gf.render_diagnosis(self.summary)
        self.assertIn("SHIP00", text)
        self.assertIn("SHIP24", text)
        self.assertIn("AG00", text)
        self.assertIn("feed=IN", text)
        self.assertIn("NOT our feed", text)


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sub", "state.json")

    def test_first_run_has_nothing_to_compare_with(self):
        summary = gf.summarize_product_views(account_rows())
        self.assertIsNone(gf.delta(summary, gf.load_state(self.path)))
        text = gf.render_diagnosis(summary, None)
        self.assertIn("no previous run", text)

    def test_second_run_reports_what_appeared_and_what_cleared(self):
        gf.save_state(gf.summarize_product_views(account_rows()), self.path)
        rows = [r for r in account_rows() if r["offerId"] != "SHIP00"]
        rows.append(_pv("NEW01", "NOT_ELIGIBLE_OR_DISAPPROVED",
                        [_issue("image_link_internal_error", "DISAPPROVED")]))
        summary = gf.summarize_product_views(rows)
        day = gf.delta(summary, gf.load_state(self.path))
        self.assertEqual(day["new"], {"image_link_internal_error": ["NEW01"]})
        self.assertEqual(
            day["cleared"],
            {"missing_shipping_mismatch_of_shipping_method_and_offer_currency":
             ["SHIP00"]})
        text = gf.render_diagnosis(summary, day)
        self.assertIn("New issues today 1", text)
        self.assertIn("Cleared since", text)

    def test_state_is_written_where_it_can_be_read_back(self):
        summary = gf.summarize_product_views(account_rows())
        self.assertTrue(gf.save_state(summary, self.path))
        with open(self.path) as fh:
            saved = json.load(fh)
        self.assertIn("missing_item_attribute_for_product_type",
                      saved["issues"])
        self.assertEqual(len(saved["issues"][
            "missing_item_attribute_for_product_type"]), 29)

    def test_an_unwritable_state_file_does_not_fail_the_report(self):
        """The 06:00 report must still be produced if it cannot journal."""
        summary = gf.summarize_product_views(account_rows())
        self.assertFalse(gf.save_state(summary, "/proc/nope/state.json"))
        self.assertEqual(gf.load_state("/proc/nope/state.json"), {})


if __name__ == "__main__":
    unittest.main()
