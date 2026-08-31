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

    def test_a_lens_with_neither_gtin_nor_mpn_is_refused(self):
        # Accepting it would mean the GMC offer claims our product_code as
        # CooperVision's identifier, which is what the frame feed does and what
        # a manufacturer's lens must never do.
        with self.assertRaises(cl_import.RowError) as caught:
            cl_import.parse_product(product(gtin="", manufacturer_mpn=""))
        self.assertIn("GTIN", str(caught.exception))

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

    def test_it_commits_per_product(self):
        # One transaction per product: a product whose matrix fails leaves
        # nothing behind and does not roll back the ones that succeeded.
        loop = self.src.split("for product in products:", 1)[1]
        self.assertIn("db.rollback()", loop)
        self.assertIn("db.commit()", loop)


if __name__ == "__main__":
    unittest.main()
