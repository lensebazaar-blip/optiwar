"""Tests for the contact-lens daily-report section.

DB-free: the section must render without a database, must never invent a live
count, and must report ``.in`` exposure as an invariant — RED and CRITICAL the
moment a single lens row is sellable there.

The release verdict is deliberately taken from the application's own
``catalogue.lens_release_blockers``, so these tests load that module the same way
the section does and assert the section quotes it rather than re-deriving
"released".
"""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(__file__)
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lens_report_section = _load("lens_report_section",
                            "reports/lens_report_section.py")
report_executive = _load("report_executive", "reports/report_executive.py")

_DB_VARS = ("ACR_REPORT_DB_HOST", "ACR_REPORT_DB_USER", "ACR_REPORT_DB_PASS",
            "ACR_REPORT_DB_NAME", "MYSQL_HOST", "MYSQL_USER",
            "MYSQL_PASSWORD", "MYSQL_DB", "MYSQL_DATABASE")


def _released(**over):
    """A lens row with nothing blocking it."""
    row = {
        "product_id": 2001, "product_code": "CL-CV-MDT30",
        "product_name": "MyDay Toric", "product_slug": "myday-toric",
        "product_image": "myday.jpg", "product_status": "ACTIVE",
        "product_vertical": "CONTACT_LENS", "sell_on_com": 1, "sell_on_in": 0,
        "product_price_eur": "45.00", "product_special_price_eur": "39.90",
        "brand": "CooperVision", "manufacturer": "CooperVision",
        "gtin": "5010000000000", "manufacturer_mpn": "MDT-30",
        "modality": "DAILY", "lens_type": "TORIC", "availability": "IN_STOCK",
        "lead_time_days": None, "merchant_enabled": 1,
        "min_boxes_single_eye": 1, "min_boxes_both_per_eye": 1,
        "variant_count": 120, "image_count": 2,
    }
    row.update(over)
    return row


class TestGate(unittest.TestCase):
    """The section must not own a second definition of 'released'."""

    def setUp(self):
        self.s = lens_report_section
        self.s._reset_cache()
        self.gate = self.s.load_gate(_ROOT)

    def test_gate_is_the_applications_own_function(self):
        self.assertEqual(self.gate.__name__, "lens_release_blockers")

    def test_a_complete_row_is_live_and_an_unreleased_one_is_not(self):
        live, held, reasons = self.s.blocker_tally(
            [_released(), _released(product_code="X", merchant_enabled=0)],
            self.gate)
        self.assertEqual([r["product_code"] for r in live], ["CL-CV-MDT30"])
        self.assertEqual(reasons, {"merchant_enabled=0": 1})
        self.assertEqual(held[0][1], ("merchant_enabled=0",))

    def test_a_lens_with_no_matrix_is_held_back_not_counted_live(self):
        live, _, reasons = self.s.blocker_tally(
            [_released(variant_count=0)], self.gate)
        self.assertEqual(live, [])
        self.assertIn("no prescription matrix", reasons)

    def test_a_lens_stated_as_rules_counts_its_stated_values(self):
        # Counting combinations only would report a live rules lens as having
        # nothing orderable, and the report would disagree with the storefront.
        rows = [_released(param_mode="RULES", variant_count=0, rule_count=77)]
        live, held, _reasons = self.s.blocker_tally(rows, self.gate)
        self.assertEqual(len(live), 1)
        self.assertEqual(held, [])
        self.assertEqual(self.s.orderable_count(live), 77)

    def test_missing_gate_is_a_coverage_gap_not_a_zero(self):
        with self.assertRaises(self.s.GateUnavailable):
            self.s.load_gate("/nonexistent/app/dir")


class TestInvariant(unittest.TestCase):
    def setUp(self):
        self.s = lens_report_section
        self.s._reset_cache()

    def test_no_in_exposure_when_the_flag_is_off(self):
        self.assertEqual(self.s.in_exposure([_released()]), [])

    def test_a_single_in_sellable_lens_is_red_and_critical(self):
        rows = [_released(),
                _released(product_code="CL-AL-DT90", sell_on_in=1)]
        verdict, why = self.s.status({"rows": rows, "gate": object(),
                                      "in_exposed": self.s.in_exposure(rows)})
        self.assertEqual(verdict, self.s.RED)
        self.assertIn("CL-AL-DT90", why)

    def test_exposure_finding_is_critical(self):
        self.s._CACHE.append(({"rows": [_released(sell_on_in=1)],
                               "gate": object(),
                               "in_exposed": ["CL-AL-DT90"]}, []))
        try:
            sev = [f.severity for f in self.s.findings()]
        finally:
            self.s._reset_cache()
        self.assertIn("CRITICAL", sev)

    def test_the_invariant_line_is_printed_even_when_it_holds(self):
        self.s._CACHE.append(({"rows": [], "gate": object(), "in_exposed": [],
                               "live": [], "held": [], "reasons": {},
                               "types": {}, "on_order": [],
                               "variants_live": 0, "refusals": {},
                               "accepted": 0}, []))
        try:
            out = self.s.build()
        finally:
            self.s._reset_cache()
        self.assertIn("contact lenses exposed on optiwar.in: 0", out)
        self.assertNotIn("[RED]", out)


class TestRendering(unittest.TestCase):
    def setUp(self):
        self.s = lens_report_section
        self.s._reset_cache()
        for k in _DB_VARS:
            os.environ.pop(k, None)

    def tearDown(self):
        self.s._reset_cache()

    def test_build_never_raises_without_a_database(self):
        out = self.s.build()
        self.assertIn("CONTACT LENSES (.com)", out)

    def test_unreadable_catalogue_is_amber_not_green(self):
        out = self.s.build()
        self.assertNotIn("STATUS: GREEN", out)
        self.assertIn("coverage gap", out)

    def test_unreadable_catalogue_never_prints_a_live_count(self):
        out = self.s.build()
        self.assertIn("live n/a", out)
        self.assertIn("exposed on optiwar.in: n/a", out)

    def test_released_lenses_render_brand_type_and_matrix_size(self):
        gate = self.s.load_gate(_ROOT)
        rows = [_released(),
                _released(product_id=2002, product_code="CL-AL-DT90",
                          brand="Alcon", lens_type="SPHERICAL",
                          availability="ON_ORDER", lead_time_days=7,
                          variant_count=40)]
        live, held, reasons = self.s.blocker_tally(rows, gate)
        self.s._CACHE.append(({
            "rows": rows, "gate": gate, "live": live, "held": held,
            "reasons": reasons, "types": self.s.type_split(live),
            "on_order": [r for r in live
                         if r["availability"] == "ON_ORDER"],
            "variants_live": 160, "in_exposed": [], "refusals": {},
            "accepted": 3}, []))
        out = self.s.build()
        self.assertIn("Loaded 2 | live 2", out)
        self.assertIn("orderable combinations 160", out)
        self.assertIn("Alcon 1", out)
        self.assertIn("TORIC 1", out)
        self.assertIn("ON_ORDER (live) 1", out)
        self.assertIn("accepted 3", out)

    def test_no_prescription_values_are_selected(self):
        for column in ("sph", "cyl", "axis", "add_power"):
            self.assertNotIn(column, self.s.LENS_ROWS_SQL)

    def test_matrix_refusals_are_counted_by_reason_code(self):
        self.s._CACHE.append(({
            "rows": [_released()], "gate": object(), "live": [_released()],
            "held": [], "reasons": {}, "types": {"TORIC": 1},
            "on_order": [], "variants_live": 120, "in_exposed": [],
            "refusals": {"COMBINATION_NOT_MADE": 4, "NO_BOXES_CHOSEN": 1},
            "accepted": 2}, []))
        out = self.s.build()
        self.assertIn("refused by the matrix 5", out)
        self.assertIn("COMBINATION_NOT_MADE", out)
        sev = [(f.severity, f.message) for f in self.s.findings()]
        self.assertTrue(any("not in the loaded matrix" in msg
                            for _, msg in sev))


class TestTypeGrouping(unittest.TestCase):
    def test_types_are_grouped_from_the_catalogue_value(self):
        s = lens_report_section
        counts = s.type_split([
            {"lens_type": "Toric"}, {"lens_type": "toric multifocal"},
            {"lens_type": "MULTIFOCAL"}, {"lens_type": "Colour"},
            {"lens_type": "Spherical"}, {"lens_type": ""},
        ])
        self.assertEqual(counts, {"TORIC": 1, "TORIC_MULTIFOCAL": 1,
                                  "MULTIFOCAL": 1, "COLOR": 1,
                                  "SPHERICAL": 1, "unclassified": 1})


class TestWiring(unittest.TestCase):
    def test_the_executive_aggregator_expects_this_section(self):
        self.assertIn("lens", report_executive.EXPECTED_SOURCES)

    def test_the_refusal_events_the_report_reads_are_the_ones_emitted(self):
        with open(os.path.join(_ROOT, "acr.py")) as fh:
            acr_src = fh.read()
        with open(os.path.join(_ROOT, "models.py")) as fh:
            models_src = fh.read()
        self.assertIn('EV_LENS_ORDER_REFUSED = "LENS_ORDER_REFUSED"', acr_src)
        self.assertIn('EV_LENS_ORDER_VALIDATED = "LENS_ORDER_VALIDATED"',
                      acr_src)
        self.assertIn("acr.EV_LENS_ORDER_REFUSED", models_src)
        self.assertIn("acr.EV_LENS_ORDER_VALIDATED", models_src)
        with open(os.path.join(_ROOT,
                               "reports/lens_report_section.py")) as fh:
            report_src = fh.read()
        self.assertIn("LENS_ORDER_REFUSED", report_src)
        self.assertIn("LENS_ORDER_VALIDATED", report_src)

    def test_the_refused_event_carries_a_reason_code_not_a_prescription(self):
        with open(os.path.join(_ROOT, "models.py")) as fh:
            body = fh.read().split("def lens_add_to_cart(")[1].split(
                "\n@bp.route")[0]
        self.assertIn("failure_code=problems[0][0]", body)
        for field in ("_sph", "_cyl", "_axis", "_add"):
            self.assertNotIn("'%s'" % field, body)


if __name__ == "__main__":
    unittest.main()
