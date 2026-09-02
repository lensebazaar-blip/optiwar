"""The canonical commerce object: one read, and nothing it does not know.

The rules asserted here: the passport states the manufacturer's identity and
our SKU, EUR beside the stored INR and the rate that explains the pair without
re-converting either, the matrix as bounds and a count and never an enumeration,
the approved views primary-first with the merchant flag on the view, and a
release state derived from the gate — RELEASED only when nothing is
outstanding, QA_READY when the flag is the only thing left, DRAFT otherwise. It
is pure: the same row gives the same object, and JSON-LD is generated from it
rather than beside it.

    python3 -m unittest tests.test_lens_view
"""
import importlib.util
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name + "_under_test", os.path.join(REPO, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


catalogue = _load("catalogue")
lens_view = _load("lens_view")

BASE = "https://optiwar.com"

IMAGES = [
    {"url": "catalog/contact-lenses/PRECISION1/01_hero.jpg",
     "code": "01_hero", "view": "front", "alt": "Precision1 pack, front",
     "primary": True, "position": 1, "gmc": True},
    {"url": "catalog/contact-lenses/PRECISION1/04_rear.jpg",
     "code": "04_rear", "view": "rear", "alt": "Precision1 pack, end panel",
     "primary": False, "position": 2, "gmc": True},
    {"url": "catalog/contact-lenses/PRECISION1/06_detail.jpg",
     "code": "06_detail", "view": "detail", "alt": "Precision1 pack label",
     "primary": False, "position": 3, "gmc": False},
]

ROW = {
    "product_id": 3101,
    "product_code": "CL-AL-PR1D30",
    "product_name": "Precision1",
    "product_slug": "alcon-precision1-30",
    "product_image": "catalog/contact-lenses/PRECISION1/01_hero.jpg",
    "product_status": "ACTIVE",
    "product_vertical": "CONTACT_LENS",
    "sell_on_com": 1,
    "sell_on_in": 0,
    "product_price_eur": "26.95",
    "product_special_price_eur": "15.11",
    "product_price": "2479.00",
    "product_special_price": "1390.00",
    "eur_inr_rate": "92.0000",
    "eur_inr_rate_at": "2026-09-02 10:00:00",
    "brand": "Alcon",
    "manufacturer": "Alcon",
    "gtin": "",
    "manufacturer_mpn": "",
    "modality": "DAILY",
    "lens_type": "SPHERICAL",
    "pack_quantity": 30,
    "material": "verofilcon A",
    "water_content": "51.00",
    "silicone_hydrogel": 1,
    "replacement_days": 1,
    "availability": "IN_STOCK",
    "lead_time_days": None,
    "expected_available_at": None,
    "prescription_required": 1,
    "color_enabled": 0,
    "merchant_enabled": 1,
    "param_mode": "MATRIX",
    "param_source": "ALCON_PRECISION1_2026",
    "min_boxes_single_eye": 1,
    "min_boxes_both_per_eye": 1,
    "variant_count": 35,
    "rule_count": 0,
    "image_count": 3,
    "images": IMAGES,
}

MATRIX = {"variants": 35, "sph_min": "-12.00", "sph_max": "6.00",
          "cyl_min": None, "cyl_max": None, "axis_min": None, "axis_max": None,
          "add_min": None, "add_max": None,
          "bc_min": "8.30", "bc_max": "8.30",
          "dia_min": "14.20", "dia_max": "14.20",
          "colors": []}


def _row(**changes):
    row = dict(ROW, **changes)
    row["release_blockers"] = catalogue.lens_release_blockers(
        row, catalogue.SITE_COM)
    return row


def _passport(**changes):
    row = _row(**changes)
    return lens_view.passport(row, MATRIX, BASE)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.data = _passport()

    def test_the_identity_is_the_manufacturers_and_the_sku_is_ours(self):
        self.assertEqual(self.data["identity"]["brand"], "Alcon")
        self.assertEqual(self.data["identity"]["manufacturer"], "Alcon")
        self.assertEqual(self.data["product"]["code"], "CL-AL-PR1D30")

    def test_no_identifier_is_invented_when_the_supplier_holds_none(self):
        # identifier_exists=false is the honest submission; a product_code
        # presented as the manufacturer's part number is not.
        self.assertIsNone(self.data["identity"]["gtin"])
        self.assertIsNone(self.data["identity"]["mpn"])
        self.assertFalse(self.data["identity"]["identifier_exists"])

    def test_an_identifier_that_exists_is_stated(self):
        data = _passport(gtin="8888888888888", manufacturer_mpn="PR1D30")
        self.assertEqual(data["identity"]["gtin"], "8888888888888")
        self.assertTrue(data["identity"]["identifier_exists"])

    def test_the_lens_facts_are_the_ones_printed_on_the_carton(self):
        lens = self.data["lens"]
        self.assertEqual(lens["modality"], "DAILY")
        self.assertEqual(lens["pack_quantity"], 30)
        self.assertEqual(lens["material"], "verofilcon A")
        self.assertEqual(lens["water_content"], "51")
        self.assertTrue(lens["silicone_hydrogel"])
        self.assertTrue(lens["prescription_required"])

    def test_the_vertical_is_com_only(self):
        self.assertEqual(self.data["product"]["sites"],
                         {"com": True, "in": False})


class PriceTests(unittest.TestCase):
    def setUp(self):
        self.price = _passport()["price"]

    def test_eur_is_the_selling_price_and_the_currency_is_stated(self):
        self.assertEqual(self.price["currency"], "EUR")
        self.assertEqual(self.price["list"], "26.95")
        self.assertEqual(self.price["selling"], "15.11")

    def test_inr_is_the_stored_shelf_price_and_is_never_recomputed(self):
        # 15.11 x 92 = 1390.12; the shelf says 1390 because a person converted
        # once and recorded the rate. Recomputing here would invent 1390.12.
        self.assertEqual(self.price["inr"]["list"], "2479")
        self.assertEqual(self.price["inr"]["selling"], "1390")
        self.assertEqual(self.price["inr"]["rate"], "92")
        self.assertTrue(self.price["inr"]["rate_at"])

    def test_a_lens_with_no_discount_sells_at_its_list_price(self):
        price = _passport(product_special_price_eur=None,
                          product_special_price=None)["price"]
        self.assertEqual(price["selling"], "26.95")
        self.assertEqual(price["inr"]["selling"], "2479")


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.matrix = _passport()["ordering"]["matrix"]

    def test_the_matrix_is_bounds_and_a_count(self):
        self.assertEqual(self.matrix["variants"], 35)
        self.assertEqual(self.matrix["sph"], {"min": "-12", "max": "6"})
        self.assertEqual(self.matrix["base_curve"],
                         {"min": "8.3", "max": "8.3"})

    def test_a_parameter_the_lens_does_not_have_is_absent_not_null(self):
        # A spherical lens has no cylinder. Emitting cyl: {min: null} invites a
        # caller to render an axis control for a lens that has no axis.
        self.assertNotIn("cyl", self.matrix)
        self.assertNotIn("axis", self.matrix)

    def test_the_passport_cannot_be_used_to_build_a_selection(self):
        # Only the stored rows say what is orderable; bounds say nothing about
        # the step or the holes a manufacturer leaves in a range.
        self.assertNotIn("values", self.matrix)
        self.assertNotIn("combinations", self.matrix)
        blob = json.dumps(self.matrix)
        self.assertNotIn("-11.75", blob)

    def test_the_ordering_minimums_are_the_suppliers(self):
        ordering = _passport()["ordering"]
        self.assertEqual(ordering["min_boxes_single_eye"], 1)
        self.assertEqual(ordering["min_boxes_both_per_eye"], 1)
        self.assertEqual(ordering["param_source"], "ALCON_PRECISION1_2026")


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.images = _passport()["images"]

    def test_every_approved_view_is_present_primary_first(self):
        self.assertEqual([i["code"] for i in self.images],
                         ["01_hero", "04_rear", "06_detail"])
        self.assertTrue(self.images[0]["primary"])
        self.assertEqual([i["position"] for i in self.images], [1, 2, 3])

    def test_a_view_carries_its_own_alt_text_and_absolute_url(self):
        self.assertEqual(self.images[0]["alt"], "Precision1 pack, front")
        self.assertEqual(
            self.images[0]["url"],
            BASE + "/static/catalog/contact-lenses/PRECISION1/01_hero.jpg")
        self.assertEqual(self.images[0]["path"],
                         "catalog/contact-lenses/PRECISION1/01_hero.jpg")

    def test_the_merchant_flag_travels_with_the_view(self):
        # The label sample states one physical box's power against an offer
        # covering the whole matrix, so it is a PDP image and not offer imagery.
        self.assertTrue(self.images[0]["gmc"])
        self.assertFalse(self.images[-1]["gmc"])

    def test_a_row_written_before_the_metadata_existed_still_renders(self):
        data = _passport(images=["catalog/contact-lenses/X/old.jpg"])
        image = data["images"][0]
        self.assertTrue(image["primary"])
        self.assertTrue(image["gmc"])
        self.assertEqual(image["alt"], "")

    def test_a_gallery_always_has_exactly_one_primary(self):
        rows = [dict(i, primary=False) for i in IMAGES]
        data = _passport(images=rows)
        self.assertEqual(sum(1 for i in data["images"] if i["primary"]), 1)


class ReleaseStateTests(unittest.TestCase):
    def test_released_when_the_gate_reports_nothing_outstanding(self):
        data = _passport()
        self.assertEqual(data["release"]["blockers"], [])
        self.assertEqual(data["release"]["state"], lens_view.STATE_RELEASED)
        self.assertTrue(data["release"]["merchant_enabled"])

    def test_qa_ready_when_the_flag_is_the_only_thing_left(self):
        data = _passport(merchant_enabled=0)
        self.assertEqual(data["release"]["blockers"],
                         [lens_view.NOT_RELEASED])
        self.assertEqual(data["release"]["state"], lens_view.STATE_QA_READY)
        self.assertFalse(data["release"]["merchant_enabled"])

    def test_draft_while_any_real_work_is_outstanding(self):
        data = _passport(merchant_enabled=0, product_slug="")
        self.assertEqual(data["release"]["state"], lens_view.STATE_DRAFT)
        self.assertGreater(len(data["release"]["blockers"]), 1)

    def test_a_released_flag_on_an_unfinished_lens_is_still_draft(self):
        # The flag is a decision, not a state: the gate outranks it.
        data = _passport(availability="")
        self.assertTrue(data["release"]["merchant_enabled"])
        self.assertEqual(data["release"]["state"], lens_view.STATE_DRAFT)

    def test_the_state_is_derived_and_not_stored(self):
        self.assertNotIn("lifecycle", _passport())
        self.assertNotIn("state", _passport()["product"])


class ObjectTests(unittest.TestCase):
    def test_the_object_is_json_serialisable_and_versioned(self):
        data = _passport()
        self.assertEqual(data["schema_version"], lens_view.SCHEMA_VERSION)
        self.assertEqual(json.loads(lens_view.as_json(data))["product"]["id"],
                         3101)

    def test_the_same_row_gives_the_same_object(self):
        self.assertEqual(lens_view.as_json(_passport()),
                         lens_view.as_json(_passport()))

    def test_the_seo_block_is_the_canonical_com_url_and_the_description(self):
        seo = _passport()["seo"]
        self.assertEqual(seo["canonical"],
                         "https://optiwar.com/categories/contact-lenses/"
                         "alcon-precision1-30?pid=3101")
        self.assertIn("Alcon", seo["description"])
        self.assertIn("Contact Lenses", seo["product_type"])

    def test_jsonld_is_generated_from_the_same_row(self):
        row = _row()
        blocks = lens_view.jsonld(row, MATRIX, BASE)
        product = next(json.loads(b) for b in blocks
                       if '"Product"' in b)
        data = lens_view.passport(row, MATRIX, BASE)
        self.assertEqual(product["sku"], data["product"]["code"])
        self.assertEqual(product["offers"]["price"], data["price"]["selling"])
        self.assertEqual(product["brand"]["name"],
                         data["identity"]["brand"])

    def test_jsonld_carries_every_approved_view_and_the_feed_does_not(self):
        row = _row()
        blocks = lens_view.jsonld(row, MATRIX, BASE)
        product = next(json.loads(b) for b in blocks if '"Product"' in b)
        self.assertEqual(len(product["image"]), 3)


if __name__ == "__main__":
    unittest.main()
