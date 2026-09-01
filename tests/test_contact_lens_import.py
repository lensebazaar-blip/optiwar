"""The importer's job is to refuse, so these tests are mostly refusals.

The rows arrive in another company's spreadsheet and become products people buy
with a prescription, so every check here exists because the alternative is
selling somebody a lens that does not exist or is not the one they need.

    python3 -m unittest tests.test_contact_lens_import
"""
import decimal
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cl_import = _load("cl_import_under_test", os.path.join(REPO, "cl_import.py"))


def product(**over):
    row = {"source_ref": "LB-1001", "manufacturer": "CooperVision",
           "brand": "MyDay", "product_name": "MyDay Toric 30 Pack",
           "gtin": "5060502210012", "manufacturer_mpn": "MDT-30",
           "modality": "Daily", "lens_type": "Toric", "pack_quantity": "30",
           "material": "stenfilcon A", "water_content": "54",
           "replacement_days": "1", "availability": "IN_STOCK",
           "price_eur": "39.90", "image_url": "https://x/myday-toric.jpg",
           "description": "Daily toric lens."}
    row.update(over)
    return row


def variant(**over):
    row = {"source_ref": "LB-1001", "sph": "-4.50", "cyl": "-1.25",
           "axis": "180", "base_curve": "8.6", "diameter": "14.5"}
    row.update(over)
    return row


class ProductRowTest(unittest.TestCase):
    def test_a_complete_row_parses(self):
        parsed = cl_import.parse_product(product())
        self.assertEqual(parsed["manufacturer"], "CooperVision")
        self.assertEqual(parsed["lens_type"], "TORIC")
        self.assertEqual(parsed["modality"], "DAILY")
        self.assertEqual(parsed["pack_quantity"], 30)
        self.assertEqual(parsed["price_eur"], decimal.Decimal("39.90"))
        self.assertEqual(parsed["source_system"], cl_import.SOURCE_SYSTEM)

    def test_a_lens_nobody_holds_an_identifier_for_still_imports(self):
        # The supplier holds neither for any pilot lens. That is the ordinary
        # case, and the honest submission is identifier_exists=false; refusing
        # the import would only invite somebody to type a code in.
        parsed = cl_import.parse_product(product(gtin="",
                                                 manufacturer_mpn=""))
        self.assertEqual(parsed["gtin"], "")
        self.assertEqual(parsed["manufacturer_mpn"], "")

    def test_the_products_own_name_in_the_mpn_column_is_refused(self):
        # What the export actually contains: manufacturer_mpn = "MyDay Torics
        # 30 Pack". Sending it would claim a manufacturer part number that
        # belongs to whatever product genuinely carries that code.
        for fake in ("MyDay Toric 30 Pack", "LB-1001"):
            with self.assertRaises(cl_import.RowError) as caught:
                cl_import.parse_product(product(gtin="",
                                                manufacturer_mpn=fake))
            self.assertIn("not a manufacturer part number",
                          str(caught.exception))

    def test_a_lens_type_we_do_not_model_is_refused(self):
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_product(product(lens_type="Scleral"))

    def test_out_of_stock_is_not_an_availability_a_lens_has(self):
        # A lens is replenished, so it is IN_STOCK or ON_ORDER. Accepting
        # OUT_OF_STOCK is how frame inventory logic would start deciding
        # whether a lens can be sold.
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_product(product(availability="OUT_OF_STOCK"))

    def test_on_order_without_a_lead_time_is_refused(self):
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_product(product(availability="ON_ORDER"))
        parsed = cl_import.parse_product(product(availability="ON_ORDER",
                                                lead_time_days="5"))
        self.assertEqual(parsed["lead_time_days"], 5)

    def test_a_missing_price_or_image_is_refused(self):
        for field in ("price_eur", "image_url", "manufacturer", "brand"):
            with self.assertRaises(cl_import.RowError):
                cl_import.parse_product(product(**{field: ""}))

    def test_a_discount_above_the_price_is_refused(self):
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_product(product(special_price_eur="49.90"))


class VariantRowTest(unittest.TestCase):
    def test_a_toric_row_parses(self):
        parsed = cl_import.parse_variant(variant(), "TORIC")
        self.assertEqual(parsed["sph"], decimal.Decimal("-4.50"))
        self.assertEqual(parsed["cyl"], decimal.Decimal("-1.25"))
        self.assertEqual(parsed["axis"], 180)
        self.assertEqual(parsed["available"], 1)

    def test_a_toric_row_without_an_axis_is_refused(self):
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_variant(variant(axis=""), "TORIC")

    def test_a_spherical_row_carrying_a_cylinder_is_refused(self):
        # The value landed in the wrong column, and importing it would offer a
        # cylinder on a lens that has none.
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_variant(variant(cyl="-1.25", axis=""), "SPHERICAL")

    def test_a_multifocal_row_needs_an_add(self):
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_variant(variant(cyl="", axis=""), "MULTIFOCAL")
        parsed = cl_import.parse_variant(
            variant(cyl="", axis="", add_power="2.00"), "MULTIFOCAL")
        self.assertEqual(parsed["add_power"], decimal.Decimal("2.00"))

    def test_plus_form_cylinder_is_refused(self):
        # Manufacturers state minus cylinder. A transposed sign is a different
        # lens, not a different notation we can accept quietly.
        with self.assertRaises(cl_import.RowError) as caught:
            cl_import.parse_variant(variant(cyl="1.25"), "TORIC")
        self.assertIn("minus-cylinder", str(caught.exception))

    def test_a_power_off_the_quarter_step_is_refused(self):
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_variant(variant(sph="-4.30"), "TORIC")

    def test_an_axis_outside_the_dial_is_refused(self):
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_variant(variant(axis="200"), "TORIC")

    def test_plano_is_a_sphere_power_and_a_blank_is_not(self):
        self.assertEqual(
            cl_import.parse_variant(variant(sph="0.00"), "TORIC")["sph"],
            decimal.Decimal("0.00"))
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_variant(variant(sph=""), "TORIC")

    def test_a_colour_lens_needs_a_colour_and_others_must_not_have_one(self):
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_variant(variant(cyl="", axis=""), "COLOR")
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_variant(variant(color_code="HAZEL"), "TORIC")

    def test_a_withdrawn_combination_parses_as_unavailable(self):
        self.assertEqual(
            cl_import.parse_variant(variant(available="no"),
                                    "TORIC")["available"], 0)


class ExportTest(unittest.TestCase):
    def test_a_valid_export_yields_the_product_with_its_matrix(self):
        products, errors = cl_import.parse(
            [product()],
            [variant(), variant(sph="-4.75"), variant(sph="-5.00", axis="90")])
        self.assertEqual(errors, [])
        self.assertEqual(len(products), 1)
        self.assertEqual(len(products[0]["variants"]), 3)

    def test_a_range_is_not_a_matrix(self):
        """The four sphere/cylinder numbers in the brief describe 164 possible
        combinations; the export states which of them CooperVision makes. This
        is the shape of that: only stated rows exist, and nothing multiplies the
        minima and maxima to invent the rest."""
        products, _errors = cl_import.parse(
            [product()], [variant(sph="-4.50", cyl="-1.25", axis="180"),
                          variant(sph="-4.50", cyl="-2.25", axis="20")])
        combinations = {(str(v["sph"]), str(v["cyl"]), v["axis"])
                        for v in products[0]["variants"]}
        self.assertEqual(combinations, {("-4.50", "-1.25", 180),
                                        ("-4.50", "-2.25", 20)})

    def test_a_product_whose_matrix_has_a_bad_row_is_not_imported(self):
        # Half a matrix would sell the half that loaded, so the product is held
        # back whole and the rejected row names itself.
        products, errors = cl_import.parse(
            [product()], [variant(), variant(sph="-4.30")])
        self.assertEqual(products, [])
        self.assertEqual(len(errors), 1)
        sheet, number, ref, why = errors[0]
        self.assertEqual((sheet, number, ref), ("variants", 3, "LB-1001"))
        self.assertIn("quarter-dioptre", why)

    def test_one_bad_product_does_not_hold_back_a_good_one(self):
        products, errors = cl_import.parse(
            [product(), product(source_ref="LB-1002", gtin="5060502210029",
                                lens_type="Scleral")],
            [variant(), variant(source_ref="LB-1002")])
        self.assertEqual([p["source_ref"] for p in products], ["LB-1001"])
        self.assertTrue(any("LB-1002" == ref for _s, _n, ref, _w in errors))

    def test_a_gtin_two_products_both_claim_is_refused(self):
        products, errors = cl_import.parse(
            [product(), product(source_ref="LB-1002")],
            [variant(), variant(source_ref="LB-1002")])
        self.assertEqual(products, [])
        self.assertTrue(any("also claimed by" in why
                            for _s, _n, _r, why in errors), errors)

    def test_the_same_source_ref_twice_is_refused(self):
        _products, errors = cl_import.parse(
            [product(), product(gtin="5060502210029")], [variant()])
        self.assertTrue(any("twice" in why for _s, _n, _r, why in errors),
                        errors)

    def test_a_duplicated_combination_is_reported_against_its_row(self):
        _products, errors = cl_import.parse([product()],
                                            [variant(), variant()])
        self.assertTrue(any("duplicate combination" in why
                            for _s, _n, _r, why in errors), errors)

    def test_a_variant_for_a_product_not_in_the_export_is_reported(self):
        _products, errors = cl_import.parse(
            [product()], [variant(), variant(source_ref="LB-9999")])
        self.assertTrue(any(ref == "LB-9999" for _s, _n, ref, _w in errors),
                        errors)

    def test_a_product_with_nothing_orderable_is_not_imported(self):
        products, errors = cl_import.parse([product()],
                                           [variant(available="0")])
        self.assertEqual(products, [])
        self.assertTrue(any("nothing to sell" in why
                            for _s, _n, _r, why in errors), errors)

    def test_a_product_with_no_matrix_at_all_is_not_imported(self):
        products, _errors = cl_import.parse([product()], [])
        self.assertEqual(products, [])

    def test_the_signature_matches_what_the_column_computes(self):
        # The variant table enforces uniqueness on a generated column; if this
        # disagreed, a duplicate would surface as a mid-import key violation
        # instead of a named spreadsheet row.
        parsed = cl_import.parse_variant(variant(), "TORIC")
        self.assertEqual(cl_import.variant_signature(parsed),
                         "-4.50|-1.25|180|NA|8.60|14.50|")

    def test_the_report_names_every_rejection(self):
        products, errors = cl_import.parse(
            [product(), product(source_ref="LB-1002", gtin="", manufacturer_mpn="")],
            [variant()])
        text = cl_import.report(products, errors)
        self.assertIn("LB-1001", text)
        self.assertIn("REJECT", text)
        self.assertIn("LB-1002", text)


def rules_product(**over):
    row = product(param_mode="RULES",
                  param_source="LensBazaar EU ordering rule 2026-09")
    row.update(over)
    return row


def rule(**over):
    row = {"source_ref": "LB-1001", "parameter": "sph", "value": "-9.00"}
    row.update(over)
    return row


MYDAY_CYLINDERS = ("-0.75", "-1.25", "-1.75", "-2.25")


def myday_rules():
    """MyDay Toric as the source states it: three lists, and no combinations.

    The supplied PWR range is a quarter-step run, so the powers come from the
    source's own list of selectable values; nothing here multiplies the three
    lists together, which is the 4,032-row chart CooperVision never published.
    """
    powers = ["%.2f" % (-9.00 + 0.25 * step) for step in range(53)]
    rows = [rule(parameter="sph", value=p) for p in powers]
    rows += [rule(parameter="cyl", value=c) for c in MYDAY_CYLINDERS]
    rows += [rule(parameter="axis", value=str(a))
             for a in range(10, 181, 10)]
    rows += [rule(parameter="bc", value="8.6"), rule(parameter="dia",
                                                    value="14.5")]
    return rows


class RuleRowTest(unittest.TestCase):
    def test_a_value_parses_with_its_parameter_named_as_the_sheet_writes_it(self):
        self.assertEqual(cl_import.parse_rule(rule(parameter="PWR"))["parameter"],
                         "sph")
        self.assertEqual(cl_import.parse_rule(rule(parameter="BC",
                                                   value="8.6"))["parameter"],
                         "base_curve")

    def test_a_parameter_we_do_not_model_is_refused(self):
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_rule(rule(parameter="tint_depth", value="3"))

    def test_a_power_off_the_quarter_step_is_refused(self):
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_rule(rule(value="-4.30"))

    def test_a_plus_form_cylinder_is_refused(self):
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_rule(rule(parameter="cyl", value="+1.25"))

    def test_an_axis_outside_the_dial_is_refused(self):
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_rule(rule(parameter="axis", value="200"))


class RulesExportTest(unittest.TestCase):
    """A product whose source states parameters, not combinations."""

    def test_myday_toric_imports_as_stated_values_not_as_a_matrix(self):
        products, errors = cl_import.parse([rules_product()], [],
                                           myday_rules())
        self.assertEqual(errors, [])
        stated = products[0]["rules"]
        # 53 powers + 4 cylinders + 18 axes + BC + DIA. Not 53 x 4 x 18.
        self.assertEqual(len(stated), 77)
        self.assertEqual({r["parameter"] for r in stated},
                         {"sph", "cyl", "axis", "base_curve", "diameter"})
        self.assertEqual(products[0]["variants"], [])

    def test_a_rules_product_that_also_carries_combinations_is_refused(self):
        products, errors = cl_import.parse([rules_product()], [variant()],
                                           myday_rules())
        self.assertEqual(products, [])
        self.assertTrue(any("RULES with combination rows" in why
                            for _s, _n, _r, why in errors), errors)

    def test_a_matrix_product_that_also_carries_parameter_rows_is_refused(self):
        products, errors = cl_import.parse([product()], [variant()],
                                           [rule()])
        self.assertEqual(products, [])
        self.assertTrue(any("MATRIX with parameter rows" in why
                            for _s, _n, _r, why in errors), errors)

    def test_a_toric_stated_without_axes_is_refused(self):
        rows = [r for r in myday_rules() if r["parameter"] != "axis"]
        products, errors = cl_import.parse([rules_product()], [], rows)
        self.assertEqual(products, [])
        self.assertTrue(any("no axis values" in why
                            for _s, _n, _r, why in errors), errors)

    def test_a_spherical_stated_with_cylinders_is_refused(self):
        products, errors = cl_import.parse(
            [rules_product(lens_type="Spherical")], [],
            [rule(), rule(parameter="cyl", value="-1.25")])
        self.assertEqual(products, [])
        self.assertTrue(any("with cyl values" in why
                            for _s, _n, _r, why in errors), errors)

    def test_rules_without_a_named_source_are_refused(self):
        # An assertion that every combination of the lists is orderable has to
        # say whose assertion it is.
        products, errors = cl_import.parse(
            [rules_product(param_source="")], [], myday_rules())
        self.assertEqual(products, [])
        self.assertTrue(any("param_source" in why
                            for _s, _n, _r, why in errors), errors)

    def test_the_minimum_boxes_are_the_products_own(self):
        parsed = cl_import.parse_product(
            rules_product(min_boxes_single_eye="8", min_boxes_both_per_eye="4"))
        self.assertEqual(parsed["min_boxes_single_eye"], 8)
        self.assertEqual(parsed["min_boxes_both_per_eye"], 4)

    def test_no_minimum_is_stated_as_no_minimum(self):
        parsed = cl_import.parse_product(rules_product())
        self.assertIsNone(parsed["min_boxes_single_eye"])
        self.assertIsNone(parsed["min_boxes_both_per_eye"])

    def test_the_manufacturers_own_name_is_published_and_the_sources_is_kept(self):
        parsed = cl_import.parse_product(
            product(manufacturer="Johnsons and Johnsons"))
        self.assertEqual(parsed["manufacturer"], "Johnson & Johnson Vision")
        self.assertEqual(parsed["source_manufacturer"],
                         "Johnsons and Johnsons")

    def test_the_dry_run_counts_values_not_invented_combinations(self):
        products, errors = cl_import.parse([rules_product()], [],
                                           myday_rules())
        text = cl_import.report(products, errors)
        self.assertIn("77 stated value(s) in 5 parameter(s)", text)


class ImporterContractTest(unittest.TestCase):
    """Properties of the writing script that must not quietly change."""

    def setUp(self):
        path = os.path.join(REPO, "scripts", "import_contact_lenses.py")
        with open(path, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_it_never_deletes(self):
        # A combination a manufacturer withdraws becomes available = 0, because
        # an order line that pointed at it must stay readable.
        self.assertNotIn("DELETE FROM", self.src.upper())
        self.assertNotIn("TRUNCATE", self.src.upper())

    def test_it_writes_only_on_apply(self):
        self.assertIn("--apply", self.src)
        self.assertIn("DRY RUN", self.src)

    def test_it_does_not_release_what_it_imports(self):
        # merchant_enabled must not appear in the upsert: an import puts a lens
        # in the database, and a person puts it on a surface.
        upsert = self.src.split("def upsert_profile", 1)[1].split("def ", 1)[0]
        self.assertNotIn("\"merchant_enabled\"", upsert)

    def test_it_loads_the_vertical_off_and_india_off(self):
        self.assertIn("\"sell_on_com\": 1", self.src)
        self.assertIn("\"sell_on_in\": 0", self.src)

    def test_the_rupee_price_is_derived_from_euro_at_a_stated_rate(self):
        # EUR is what the supplier quotes; INR is derived once, at a rate the
        # run was given and records, and never from a previous conversion.
        script = _load("import_contact_lenses_under_test",
                       os.path.join(REPO, "scripts",
                                    "import_contact_lenses.py"))
        self.assertEqual(script.in_rupees("25.91", 94.5), 2448.49)
        self.assertIsNone(script.in_rupees("25.91", None))
        self.assertIn("eur_inr_rate", self.src)

    def test_it_commits_per_product(self):
        # One transaction per product: a product whose matrix fails leaves
        # nothing behind and does not roll back the ones that succeeded.
        loop = self.src.split("for product in products:", 1)[1]
        self.assertIn("db.rollback()", loop)
        self.assertIn("db.commit()", loop)


if __name__ == "__main__":
    unittest.main()
