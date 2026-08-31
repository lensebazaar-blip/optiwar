"""What search and answer engines are told about a contact lens.

One flag decides every surface. The rules asserted here: a released lens has a
canonical .com URL, Product and Breadcrumb JSON-LD carrying the manufacturer's
identity, a sitemap entry, an image sitemap entry, a shelf in the category tree
and a brand page; an unreleased lens has none of them; optiwar.in has none of
them whatever the rows say; and no page exists per power/cylinder/axis.

    python3 -m unittest tests.test_lens_seo
"""
import importlib.util
import json
import os
import sys
import unittest

from jinja2 import (ChoiceLoader, DictLoader, Environment, FileSystemLoader,
                    select_autoescape)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name + "_under_test", os.path.join(REPO, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lens_seo = _load("lens_seo")
catalogue = _load("catalogue")

BASE = "https://optiwar.com"

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
    "images": ["myday_toric_30.jpg", "myday_toric_box.jpg"],
    "release_blockers": (),
}

MONTHLY = dict(LIVE, product_id=2002, product_code="CL-AL-TOT30",
               product_name="Total30", brand="Alcon", manufacturer="Alcon",
               gtin="8888888888888", manufacturer_mpn="TOT30",
               modality="MONTHLY", lens_type="SPHERICAL",
               product_slug="alcon-total30-30",
               images=["total30.jpg"])

MATRIX = {"variants": 84, "sph_min": "-10.00", "sph_max": "6.00",
          "cyl_min": "-2.25", "cyl_max": "-0.75",
          "axis_min": "10", "axis_max": "180",
          "add_min": None, "add_max": None}


def _blocked(**changes):
    row = dict(LIVE, **changes)
    row["release_blockers"] = catalogue.lens_release_blockers(
        row, catalogue.SITE_COM)
    return row


class ProductJsonLdTests(unittest.TestCase):
    def setUp(self):
        self.data = lens_seo.product_jsonld(LIVE, BASE, MATRIX)

    def test_the_identity_is_the_manufacturers_and_the_sku_is_ours(self):
        # The frame page sends brand=Optiwar and mpn=product_code. Saying that
        # about a CooperVision box claims we made it.
        self.assertEqual(self.data["brand"], {"@type": "Brand",
                                              "name": "CooperVision"})
        self.assertEqual(self.data["mpn"], "MDT-30")
        self.assertEqual(self.data["gtin"], "5060138341234")
        self.assertEqual(self.data["sku"], "CL-CV-MDT30")
        self.assertEqual(self.data["manufacturer"]["name"], "CooperVision")

    def test_the_offer_is_the_eur_box_price_and_the_canonical_com_url(self):
        offer = self.data["offers"]
        self.assertEqual(offer["priceCurrency"], "EUR")
        self.assertEqual(offer["price"], "39.90")
        url = ("https://optiwar.com/categories/contact-lenses/"
               "coopervision-myday-toric-30?pid=2001")
        self.assertEqual(offer["url"], url)
        self.assertEqual(self.data["url"], url)

    def test_in_stock_and_on_order_are_the_only_availabilities(self):
        self.assertEqual(self.data["offers"]["availability"],
                         "https://schema.org/InStock")
        ordered = lens_seo.product_jsonld(
            dict(LIVE, availability="ON_ORDER", lead_time_days=10), BASE)
        self.assertEqual(ordered["offers"]["availability"],
                         "https://schema.org/BackOrder")
        self.assertIn("availabilityStarts", ordered["offers"])
        # A lens is replenished, never depleted: frame quantity says nothing.
        for row in (LIVE, dict(LIVE, product_quantity=0)):
            self.assertNotIn(
                "OutOfStock",
                json.dumps(lens_seo.product_jsonld(row, BASE)))

    def test_it_states_the_lens_facts_and_the_matrix_it_actually_holds(self):
        props = {p["name"]: p["value"]
                 for p in self.data["additionalProperty"]}
        self.assertEqual(props["Sphere powers"], "-10 to 6")
        self.assertEqual(props["Cylinder powers"], "-2.25 to -0.75")
        self.assertEqual(props["Prescription combinations available"], "84")
        self.assertNotIn("Add powers", props)   # nothing to state
        self.assertEqual(props["Replacement schedule"], "Daily disposable")
        self.assertEqual(props["Lens type"], "Toric")
        self.assertEqual(props["Pack size"], "30 lenses")

    def test_it_makes_no_frame_promise_and_no_shipping_claim(self):
        blob = json.dumps(self.data)
        for claim in ("Complimentary prescription lenses", "Frame Material",
                      "Frame Shape", "shippingDetails", "returnPolicy",
                      "aggregateRating", "HowTo"):
            self.assertNotIn(claim, blob)

    def test_the_images_are_absolute_and_the_primary_is_first(self):
        self.assertEqual(self.data["image"],
                         ["https://optiwar.com/myday_toric_30.jpg",
                          "https://optiwar.com/myday_toric_box.jpg"])

    def test_an_unreleased_or_unpriced_lens_has_no_markup(self):
        self.assertIsNone(lens_seo.product_jsonld(
            _blocked(merchant_enabled=0), BASE))
        self.assertIsNone(lens_seo.product_jsonld(
            dict(LIVE, product_price_eur="", product_special_price_eur=""),
            BASE))
        self.assertEqual(lens_seo.jsonld_blocks(_blocked(merchant_enabled=0),
                                                BASE), [])


class BreadcrumbTests(unittest.TestCase):
    def test_the_trail_is_home_lenses_facet_product(self):
        crumbs = lens_seo.breadcrumb_jsonld(LIVE, BASE)["itemListElement"]
        self.assertEqual([c["name"] for c in crumbs],
                         ["Home", "Contact Lenses", "Daily",
                          "CooperVision MyDay Toric 30 Pack"])
        self.assertEqual([c["position"] for c in crumbs], [1, 2, 3, 4])
        self.assertEqual(crumbs[2]["item"],
                         "https://optiwar.com/contact-lenses/daily")

    def test_a_product_page_emits_product_then_breadcrumb(self):
        blocks = [json.loads(b) for b in lens_seo.jsonld_blocks(LIVE, BASE)]
        self.assertEqual([b["@type"] for b in blocks],
                         ["Product", "BreadcrumbList"])


class CategoryTreeTests(unittest.TestCase):
    def test_a_shelf_exists_only_where_a_lens_is_released(self):
        pages = lens_seo.facet_pages([LIVE, MONTHLY])
        self.assertEqual([(p["slug"], len(p["rows"])) for p in pages],
                         [("daily", 1), ("monthly", 1), ("toric", 1)])
        self.assertEqual(lens_seo.facet_pages([]), [])
        # No multifocal lens is released, so no multifocal URL exists.
        self.assertNotIn("multifocal", [p["slug"] for p in pages])

    def test_brand_pages_are_the_manufacturers_actually_released(self):
        pages = lens_seo.brand_pages([LIVE, MONTHLY])
        self.assertEqual([(p["label"], p["path"]) for p in pages],
                         [("CooperVision",
                           "/contact-lenses/brand/coopervision"),
                          ("Alcon", "/contact-lenses/brand/alcon")])

    def test_no_page_exists_for_a_prescription_combination(self):
        # Thin near-duplicate URLs per power/cylinder/axis are what the matrix
        # replaces; the shelves are properties of the product, not the script.
        slugs = [entry[0] for entry in lens_seo.FACETS]
        for forbidden in ("sph", "cyl", "axis", "-4-50", "power"):
            self.assertNotIn(forbidden, slugs)
        urls = lens_seo.sitemap_urls([LIVE, MONTHLY], BASE)
        self.assertEqual(len(urls), len(
            ["root", "daily", "monthly", "toric", "cooper", "alcon"]) + 2)

    def test_a_landing_page_exists_only_for_a_released_shelf(self):
        rows = [LIVE, MONTHLY]
        self.assertIsNone(lens_seo.landing_page([], BASE))
        self.assertIsNone(lens_seo.landing_page(rows, BASE,
                                                facet_slug="multifocal"))
        self.assertIsNone(lens_seo.landing_page(rows, BASE,
                                                brand_slug="acuvue"))
        self.assertIsNone(lens_seo.landing_page(rows, BASE,
                                                facet_slug="not-a-facet"))
        page = lens_seo.landing_page(rows, BASE, facet_slug="toric")
        self.assertEqual(page["path"], "/contact-lenses/toric")
        self.assertEqual([c["title"] for c in page["rows"]],
                         ["CooperVision MyDay Toric 30 Pack"])

    def test_a_listing_entry_prices_a_box_and_states_availability(self):
        card = lens_seo.card(LIVE, BASE)
        self.assertEqual((card["price"], card["list_price"]),
                         ("39.90", "45.00"))
        self.assertEqual(card["availability"], "In stock")
        self.assertEqual(card["spec"],
                         "Daily disposable \u00b7 Toric \u00b7 box of 30")
        ordered = lens_seo.card(
            dict(LIVE, availability="ON_ORDER", lead_time_days=10), BASE)
        self.assertTrue(ordered["availability"].startswith(
            "Available on order (expected "))

    def test_the_root_page_offers_the_whole_tree(self):
        page = lens_seo.landing_page([LIVE, MONTHLY], BASE)
        self.assertEqual(page["path"], "/contact-lenses")
        self.assertEqual([s["label"] for s in page["shelves"]],
                         ["Daily", "Monthly", "Toric"])
        collection = json.loads(page["jsonld"][0])
        self.assertEqual(collection["mainEntity"]["numberOfItems"], 2)


class SitemapTests(unittest.TestCase):
    def test_every_released_lens_and_shelf_is_in_the_sitemap(self):
        urls = "\n".join(lens_seo.sitemap_urls([LIVE, MONTHLY], BASE))
        for path in ("/contact-lenses", "/contact-lenses/daily",
                     "/contact-lenses/monthly", "/contact-lenses/toric",
                     "/contact-lenses/brand/coopervision",
                     "/contact-lenses/brand/alcon",
                     "/categories/contact-lenses/coopervision-myday-toric-30"):
            self.assertIn("https://optiwar.com" + path, urls)
        self.assertIn("?pid=2001", urls.replace("&amp;", "&"))

    def test_an_unreleased_lens_is_in_no_sitemap(self):
        # live_lenses() is what feeds this; the empty case is the one that has
        # to hold, because a sitemap of shelves with nothing on them is worse
        # than no lens sitemap at all.
        self.assertEqual(lens_seo.sitemap_urls([], BASE), [])
        self.assertEqual(lens_seo.image_sitemap_urls([], BASE), [])

    def test_india_gets_no_lens_urls_whatever_the_rows_say(self):
        self.assertEqual(
            lens_seo.sitemap_urls([LIVE], BASE, is_india=True), [])
        self.assertEqual(
            lens_seo.image_sitemap_urls([LIVE], BASE, is_india=True), [])

    def test_the_shared_sitemap_file_is_stripped_of_the_new_shelf_urls(self):
        # The file is generated once for both hosts, so the .in boundary is also
        # applied on output — including the shelves this change introduces.
        xml = ("<urlset>"
               "<url><loc>https://optiwar.com/contact-lenses</loc></url>"
               "<url><loc>https://optiwar.com/contact-lenses/daily</loc></url>"
               "<url><loc>https://optiwar.com/contact-lenses/brand/alcon"
               "</loc></url>"
               "<url><loc>https://optiwar.com/spectacles</loc></url>"
               "</urlset>")
        out = catalogue.strip_ineligible_urls(xml, catalogue.SITE_IN)
        self.assertNotIn("contact-lenses", out)
        self.assertIn("spectacles", out)

    def test_the_image_sitemap_pairs_the_page_with_its_imagery(self):
        blocks = lens_seo.image_sitemap_urls([LIVE, MONTHLY], BASE)
        self.assertEqual(len(blocks), 2)
        self.assertIn("<image:loc>https://optiwar.com/myday_toric_box.jpg"
                      "</image:loc>", blocks[0])
        # A slug carrying XML metacharacters must not break the document.
        odd = lens_seo.image_sitemap_urls(
            [dict(LIVE, product_slug="a&b")], BASE)[0]
        self.assertIn("/a&amp;b?pid=2001", odd)
        self.assertEqual(
            lens_seo.image_sitemap_urls(
                [dict(LIVE, product_image="", images=[])], BASE), [])


class WiringTests(unittest.TestCase):
    """The storefront's contract with this module, read from its source."""

    def setUp(self):
        with open(os.path.join(REPO, "models.py")) as fh:
            self.src = fh.read()

    def test_the_landing_routes_exist_and_are_closed_on_india(self):
        for route in ("@bp.route('/contact-lenses')",
                      "@bp.route('/contact-lenses/<facet_slug>')",
                      "@bp.route('/contact-lenses/brand/<brand_slug>')"):
            self.assertIn(route, self.src)
        helper = self.src.split("def _lens_landing(")[1].split("\ndef ")[0]
        self.assertIn("if current_site() == SITE_IN:", helper)
        self.assertIn("live_lenses(cursor, SITE_COM)", helper)
        self.assertIn("lens_seo.landing_page(", helper)

    def test_the_legacy_listing_no_longer_publishes_a_second_set(self):
        legacy = self.src.split("def contact_lenses(")[1].split("\ndef ")[0]
        self.assertIn("redirect(lens_seo.ROOT_PATH, code=301)", legacy)
        self.assertNotIn('product_category="Contact Lenses"', legacy)

    def test_the_product_page_gates_a_lens_on_release_and_swaps_its_markup(self):
        page = self.src.split("def product_page(")[1].split("\ndef ")[0]
        self.assertIn("if is_contact_lens(product):", page)
        self.assertIn("lens_seo.jsonld_blocks(", page)
        self.assertIn("lens_matrix_summary(", page)
        self.assertIn('return "Product not found", 404', page)
        with open(os.path.join(REPO, "templates",
                               "product_page.html")) as fh:
            tpl = fh.read()
        self.assertIn("{% if lens_jsonld %}", tpl)

    def test_both_sitemaps_read_the_same_release_gate(self):
        self.assertIn("lens_seo.sitemap_urls(rows, 'https://optiwar.com')",
                      self.src)
        self.assertIn("lens_seo.image_sitemap_urls(_live_lens_rows(cur), base)",
                      self.src)
        shared = self.src.split("def _live_lens_rows(")[1].split("\ndef ")[0]
        self.assertIn("if _req_is_india():", shared)
        self.assertIn("live_lenses(cur, SITE_COM)", shared)

    def test_the_seo_module_and_template_are_deployed_with_the_routes(self):
        with open(os.path.join(REPO, "deploy", "deploy.py")) as fh:
            deploy = fh.read()
        deploy_set = deploy.split("DEPLOY_SET = (")[1].split(")")[0]
        self.assertIn('"lens_seo.py"', deploy_set)
        self.assertIn('"templates/lens_landing.html"', deploy_set)
        self.assertIn('"templates/product_page.html"', deploy_set)
        new_in = deploy.split("NEW_IN_RELEASE = (")[1].split(")")[0]
        self.assertIn('"lens_seo.py"', new_in)
        self.assertIn('"templates/lens_landing.html"', new_in)

    def test_the_landing_template_canonicalises_to_com_only(self):
        with open(os.path.join(REPO, "templates",
                               "lens_landing.html")) as fh:
            tpl = fh.read()
        self.assertIn("{% block canonical_url %}https://optiwar.com"
                      "{{ page.path }}{% endblock %}", tpl)
        # #65: a page must not name the other domain as its own alternate, so
        # both hreflang blocks are overridden to .com — base.html would
        # otherwise derive an optiwar.in alternate that 404s.
        body = "\n".join(line for line in tpl.splitlines()
                         if not line.lstrip().startswith(("{#", "alternate")))
        self.assertNotIn("optiwar.in", body)
        self.assertEqual(tpl.count("{% block hreflang_in %}"
                                   "https://optiwar.com"), 1)


class RenderTests(unittest.TestCase):
    """The templates themselves, rendered — a Jinja defect is a 500."""

    def _env(self):
        env = Environment(
            loader=ChoiceLoader([
                DictLoader({
                    "base.html": (
                        "{% block canonical_url %}{% endblock %}"
                        "{% block hreflang_in %}{% endblock %}"
                        "{% block jsonld_extra %}{% endblock %}"
                        "{% block content %}{% endblock %}"),
                    # Stubs: the imagery macros are the frames page's concern,
                    # what is under test here is the markup in the head.
                    "_picture.html": (
                        "{% macro product_picture(src, alt) %}"
                        "{{ varargs|length }}{{ kwargs|length }}"
                        "{% endmacro %}"
                        "{% macro thumb_img(src, alt) %}"
                        "{{ varargs|length }}{{ kwargs|length }}"
                        "{% endmacro %}"),
                }),
                FileSystemLoader(os.path.join(REPO, "templates")),
            ]),
            autoescape=select_autoescape(["html"]),
        )
        env.globals.update(
            versioned_image_url=lambda img, url="": "%s/static/%s" % (url, img),
            image_dimensions=lambda img: (50, 20),
            frame_shape=lambda p: "Rectangle",
            _=lambda s: s,
        )
        env.filters["_"] = lambda s: s
        return env

    def test_the_landing_page_renders_its_shelves_products_and_markup(self):
        rows = [LIVE, MONTHLY]
        view = lens_seo.landing_page(rows, BASE)
        html = self._env().get_template("lens_landing.html").render(
            page=view, brands=view["brands"], shelves=view["shelves"],
            jsonld=view["jsonld"], root_path=lens_seo.ROOT_PATH,
            is_india=False, site_url=BASE)
        self.assertIn("/contact-lenses/daily", html)
        self.assertIn("/contact-lenses/brand/coopervision", html)
        self.assertIn("MyDay Toric", html)
        self.assertIn("ItemList", html)
        self.assertNotIn("optiwar.in", html)

    def test_a_lens_product_page_carries_only_the_lens_markup(self):
        """Only the markup block: the rest of the page is the frames body."""
        tpl = self._env().get_template("product_page.html")
        ctx = tpl.new_context({
            "product": dict(LIVE, product_quantity=0),
            "lens_jsonld": lens_seo.jsonld_blocks(LIVE, BASE),
            "is_india": False, "site_url": BASE, "review_count": 0,
            "reviews": [], "avg_rating": 0,
        })
        html = "".join(tpl.blocks["jsonld_extra"](ctx))
        self.assertIn("CooperVision", html)
        self.assertIn('"@type": "BreadcrumbList"', html)
        for frame in ('"brand": "Optiwar"', "HowTo", "Frame Material",
                      "Complimentary prescription lenses"):
            self.assertNotIn(frame, html)


if __name__ == "__main__":
    unittest.main()
