"""What a contact-lens offer tells Google, and whose product it says it is.

The frame feed sends ``brand=Optiwar`` and ``mpn=product_code``. Sending that
for an Alcon box claims we manufacture it, mismatches the GTIN Google already
holds for the real product, and is the disapproval this module exists to avoid —
so the rules asserted here are: a lens offer carries the manufacturer's brand and
identifier, never ours; it exists only for a released lens; and it never appears
in the India feed.

    python3 -m unittest tests.test_lens_feed
"""
import datetime
import importlib.util
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name + "_under_test", os.path.join(REPO, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lens_feed = _load("lens_feed")
catalogue = _load("catalogue")

BASE = "https://optiwar.com"

# A released lens: the shape ``catalogue.live_lenses()`` returns, with the
# release blockers already resolved. Tests change one field at a time.
LIVE = {
    "product_id": 2001,
    "product_code": "CL-CV-MDT30",
    "product_name": "MyDay Toric",
    "product_slug": "coopervision-myday-toric-30",
    "product_image": "myday_toric_30.jpg",
    "product_status": "ACTIVE",
    "product_vertical": "CONTACT_LENS",
    "sell_on_com": 1,
    "sell_on_in": 0,
    "product_price_eur": "45.00",
    "product_special_price_eur": "39.90",
    "brand": "CooperVision",
    "manufacturer": "CooperVision",
    "gtin": "5060138341234",
    "manufacturer_mpn": "MDT-30",
    "modality": "DAILY",
    "lens_type": "TORIC",
    "pack_quantity": 30,
    "material": "stenfilcon A",
    "water_content": "54.00",
    "silicone_hydrogel": 1,
    "replacement_days": 1,
    "availability": "IN_STOCK",
    "lead_time_days": None,
    "expected_available_at": None,
    "prescription_required": 1,
    "color_enabled": 0,
    "merchant_enabled": 1,
    "variant_count": 84,
    "image_count": 2,
    "release_blockers": (),
}


def _tag(xml, tag):
    found = re.findall(r"<g:%s>(.*?)</g:%s>" % (tag, tag), xml, re.DOTALL)
    return found[0] if found else None


def _tags(xml, tag):
    return re.findall(r"<g:%s>(.*?)</g:%s>" % (tag, tag), xml, re.DOTALL)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.xml = lens_feed.lens_item_xml(LIVE, BASE)

    def test_the_brand_is_the_manufacturer_not_us(self):
        self.assertEqual(_tag(self.xml, "brand"), "CooperVision")
        self.assertNotIn("Optiwar", self.xml)

    def test_the_mpn_is_the_manufacturers_not_our_product_code(self):
        self.assertEqual(_tag(self.xml, "mpn"), "MDT-30")
        self.assertEqual(_tag(self.xml, "gtin"), "5060138341234")
        # Our code is the offer id, which is what it legitimately is.
        self.assertEqual(_tag(self.xml, "id"), "CL-CV-MDT30")

    def test_identifier_exists_is_never_claimed_false(self):
        # A manufactured lens has a real identifier; the release gate requires
        # one, so declaring none exists would be untrue.
        self.assertNotIn("identifier_exists", self.xml)

    def test_an_mpn_only_lens_sends_no_empty_gtin(self):
        xml = lens_feed.lens_item_xml(dict(LIVE, gtin=None), BASE)
        self.assertIsNone(_tag(xml, "gtin"))
        self.assertEqual(_tag(xml, "mpn"), "MDT-30")


class OfferContentTests(unittest.TestCase):
    def setUp(self):
        self.xml = lens_feed.lens_item_xml(LIVE, BASE)

    def test_title_states_brand_product_and_pack_once_each(self):
        self.assertEqual(_tag(self.xml, "title"),
                         "CooperVision MyDay Toric 30 Pack")

    def test_title_does_not_repeat_what_the_name_already_says(self):
        row = dict(LIVE, product_name="CooperVision MyDay Toric 30 Pack")
        self.assertEqual(lens_feed.lens_title(row),
                         "CooperVision MyDay Toric 30 Pack")

    def test_description_carries_only_stated_facts(self):
        desc = _tag(self.xml, "description")
        self.assertIn("Daily disposable toric contact lenses.", desc)
        self.assertIn("Box of 30 lenses.", desc)
        self.assertIn("stenfilcon A, 54% water content.", desc)
        self.assertIn("select power per eye at checkout", desc)

    def test_price_is_eur_and_the_sale_price_is_the_special_one(self):
        self.assertEqual(_tag(self.xml, "price"), "45.00 EUR")
        self.assertEqual(_tag(self.xml, "sale_price"), "39.90 EUR")

    def test_no_sale_price_when_the_special_equals_the_list_price(self):
        xml = lens_feed.lens_item_xml(
            dict(LIVE, product_special_price_eur="45.00"), BASE)
        self.assertIsNone(_tag(xml, "sale_price"))

    def test_link_and_images_are_absolute_com_urls(self):
        self.assertEqual(
            _tag(self.xml, "link"),
            "https://optiwar.com/categories/contact-lenses/"
            "coopervision-myday-toric-30?pid=2001")
        self.assertEqual(_tag(self.xml, "image_link"),
                         "https://optiwar.com/myday_toric_30.jpg")

    def test_extra_images_are_additional_links_capped_at_ten(self):
        row = dict(LIVE, images=["a%d.jpg" % i for i in range(15)])
        xml = lens_feed.lens_item_xml(row, BASE)
        self.assertEqual(len(_tags(xml, "additional_image_link")), 10)

    def test_product_type_is_the_lens_breadcrumb(self):
        self.assertEqual(lens_feed.lens_product_type(LIVE),
                         "Contact Lenses > Daily > Toric")
        # XML-escaped on the way out, as every value here is.
        self.assertEqual(_tag(self.xml, "product_type"),
                         "Contact Lenses &gt; Daily &gt; Toric")

    def test_the_specification_is_sent_as_product_detail(self):
        details = dict(zip(_tags(self.xml, "attribute_name"),
                           _tags(self.xml, "attribute_value")))
        self.assertEqual(details["Pack size"], "30 lenses")
        self.assertEqual(details["Replacement schedule"], "Daily disposable")
        self.assertEqual(details["Water content"], "54%")
        self.assertEqual(details["Material"], "stenfilcon A")
        self.assertEqual(details["Lens type"], "Toric")

    def test_base_curve_is_absent_unless_resolved_for_the_offer(self):
        # BC lives on the variants because a lens can be sold in two of them; an
        # offer-level value would be false for half the matrix.
        self.assertNotIn("Base curve", _tags(self.xml, "attribute_name"))
        xml = lens_feed.lens_item_xml(dict(LIVE, base_curve="8.60"), BASE)
        details = dict(zip(_tags(xml, "attribute_name"),
                           _tags(xml, "attribute_value")))
        self.assertEqual(details["Base curve"], "8.6 mm")

    def test_no_taxonomy_id_until_one_is_verified(self):
        self.assertIsNone(_tag(self.xml, "google_product_category"))
        os.environ["GMC_LENS_CATEGORY"] = "6600"
        try:
            xml = lens_feed.lens_item_xml(LIVE, BASE)
        finally:
            del os.environ["GMC_LENS_CATEGORY"]
        self.assertEqual(_tag(xml, "google_product_category"), "6600")


class AvailabilityTests(unittest.TestCase):
    def test_in_stock_lens_is_in_stock(self):
        self.assertEqual(lens_feed.lens_availability(LIVE), ("in_stock", ""))

    def test_on_order_is_a_backorder_with_the_stated_date(self):
        row = dict(LIVE, availability="ON_ORDER", lead_time_days=10,
                   expected_available_at=datetime.datetime(2026, 9, 15))
        self.assertEqual(lens_feed.lens_availability(row),
                         ("backorder", "2026-09-15"))

    def test_on_order_without_a_date_uses_the_lead_time(self):
        row = dict(LIVE, availability="ON_ORDER", lead_time_days=10)
        self.assertEqual(
            lens_feed.lens_availability(row, today=datetime.date(2026, 9, 1)),
            ("backorder", "2026-09-11"))

    def test_a_lens_is_never_out_of_stock(self):
        # Frame stock logic must not reach a lens: OUT_OF_STOCK is not a state a
        # replenished product has, and product_quantity is not consulted at all.
        for state in ("OUT_OF_STOCK", "", "NONSENSE"):
            xml = lens_feed.lens_item_xml(dict(LIVE, availability=state), BASE)
            self.assertEqual(_tag(xml, "availability"), "in_stock")
        self.assertNotIn("out_of_stock",
                         lens_feed.lens_item_xml(LIVE, BASE))


class ReleaseAndSiteTests(unittest.TestCase):
    def test_an_unreleased_lens_has_no_offer(self):
        blocked = dict(LIVE, merchant_enabled=0)
        blocked["release_blockers"] = catalogue.lens_release_blockers(
            blocked, catalogue.SITE_COM)
        self.assertTrue(blocked["release_blockers"])
        self.assertIsNone(lens_feed.lens_offer(blocked, BASE))
        self.assertEqual(lens_feed.lens_items([blocked], BASE), [])

    def test_a_lens_with_no_price_or_no_image_has_no_offer(self):
        for change in (dict(product_price_eur="", product_special_price_eur=""),
                       dict(product_image="", images=[])):
            self.assertIsNone(lens_feed.lens_offer(dict(LIVE, **change), BASE))

    def test_the_india_feed_has_no_lens_offers(self):
        self.assertEqual(lens_feed.lens_items([LIVE], BASE, is_india=True), [])

    def test_a_released_lens_produces_exactly_one_offer(self):
        self.assertEqual(len(lens_feed.lens_items([LIVE], BASE)), 1)


class FeedWiringTests(unittest.TestCase):
    """The feed route's contract with this module, read from its source."""

    def setUp(self):
        with open(os.path.join(REPO, "models.py")) as fh:
            self.src = fh.read()

    def test_the_frame_loop_still_skips_lenses(self):
        # Both mappings existing is the point: a lens must be emitted by exactly
        # one of them, and the frame loop's brand is ours.
        self.assertIn("if is_contact_lens(p):", self.src)
        self.assertIn("<g:brand>Optiwar</g:brand>", self.src)

    def test_the_feed_appends_lens_items_from_this_module(self):
        self.assertIn("_lens_feed_items(cur, base, is_india)", self.src)
        self.assertIn("lens_feed.lens_items(rows, base, is_india=False)",
                      self.src)

    def test_a_lens_failure_does_not_lose_the_frame_feed(self):
        # The read the feed depends on is shared with the sitemaps, so the
        # failure is contained there: 702 frames must not vanish from Merchant
        # Center because a lens table is missing.
        helper = self.src.split("def _live_lens_rows(")[1].split("\ndef ")[0]
        self.assertIn("except Exception", helper)
        self.assertIn("return []", helper)

    def test_lens_feed_is_deployed_with_the_feed_that_calls_it(self):
        with open(os.path.join(REPO, "deploy", "deploy.py")) as fh:
            deploy = fh.read()
        deploy_set = deploy.split("DEPLOY_SET = (")[1].split(")")[0]
        self.assertIn('"lens_feed.py"', deploy_set)
        new_in = deploy.split("NEW_IN_RELEASE = (")[1].split(")")[0]
        self.assertIn('"lens_feed.py"', new_in)


if __name__ == "__main__":
    unittest.main()
