import ast
import copy
import datetime
import json
import os
import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree

from jinja2 import DictLoader, Environment, select_autoescape

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import catalogue
import india_offer
import shipping_options


RETURN_COUNTRIES_COM = [
    "AU", "AT", "BH", "BE", "CA", "CY", "DK", "FI", "FR", "DE", "GR",
    "HU", "IE", "IL", "IT", "JP", "KW", "MU", "NL", "NZ", "NO", "OM",
    "PA", "PL", "PT", "RO", "SA", "SG", "ZA", "ES", "SE", "CH", "AE",
    "GB", "QA",
]

RETURN_POLICY_COM = {
    "@type": "MerchantReturnPolicy",
    "applicableCountry": RETURN_COUNTRIES_COM,
    "returnPolicyCategory": (
        "https://schema.org/MerchantReturnFiniteReturnWindow"
    ),
    "merchantReturnDays": 7,
    "returnMethod": "https://schema.org/ReturnByMail",
    "returnFees": "https://schema.org/ReturnFeesCustomerResponsibility",
    "description": (
        "Dead-on-arrival items only. Proof of damage required. Refund within "
        "1-7 business days after courier verification."
    ),
}


def _product(product_id=1, code="BB44"):
    return {
        "product_id": product_id,
        "product_code": code,
        "product_name": "OPTIWAR TEST FRAME",
        "product_details": "OPTIWAR TEST FRAME in Blue.",
        "product_category": "Spectacles Frame",
        "product_slug": "optiwar-test-frame",
        "product_image": "BB44/BB44_1.jpg,BB44/BB44_2.jpg",
        "product_quantity": 5,
        "product_status": "ACTIVE",
        "product_vertical": "EYEWEAR",
        "product_price": 2099,
        "product_special_price": 601,
        "product_price_eur": "50.99",
        "product_special_price_eur": "36.01",
        "color_display": "Blue",
        "product_color": "Blue",
        "product_size": "43-17-130",
        "product_material": "Acetate",
        "product_perception_value": "Small",
        "product_primary_color": "Blue",
        "product_secondary_color": "Blue",
        "product_diameter": "43",
        "product_bridge": "17",
        "product_lenght": "130",
        "product_gender": "Unisex",
        "product_category_kids": 0,
    }


def _source(path):
    with open(os.path.join(REPO, path), encoding="utf-8") as fh:
        return fh.read()


def _render_product(is_india, declared, source=None):
    template = source or _source("templates/product_page.html")
    env = Environment(
        loader=DictLoader({
            "base.html": "{% block jsonld_extra %}{% endblock %}",
            "_picture.html": (
                "{% macro product_picture() %}{% endmacro %}"
                "{% macro thumb_img() %}{% endmacro %}"
            ),
            "product_page.html": template,
        }),
        autoescape=select_autoescape(["html"]),
    )
    env.globals.update(
        versioned_image_url=lambda img, url="": "%s/static/%s" % (url, img),
        image_dimensions=lambda img: (50, 20),
        frame_shape=lambda product: "Rectangle",
        _=lambda value: value,
    )
    return env.get_template("product_page.html").render(
        product=_product(),
        is_india=is_india,
        site_url="https://optiwar.in" if is_india else "https://optiwar.com",
        review_count=0,
        reviews=[],
        avg_rating=0,
        inr_disc_pct=71,
        eur_disc_pct=29,
        in_ship_declared=declared,
        in_offer_shipping=india_offer.shipping_details(),
    )


def _render_home(is_india, declared, source=None):
    template = source or _source("templates/index.html")
    env = Environment(
        loader=DictLoader({
            "base.html": "{% block jsonld_extra %}{% endblock %}",
            "_picture.html": (
                "{% macro product_picture() %}{% endmacro %}"
                "{% macro thumb_img() %}{% endmacro %}"
            ),
            "index.html": template,
        }),
        autoescape=select_autoescape(["html"]),
    )
    return env.get_template("index.html").render(
        products=[_product(), _product(2, "AA01")],
        is_india=is_india,
        site_url="https://optiwar.in" if is_india else "https://optiwar.com",
        in_ship_declared=declared,
        in_offer_shipping=india_offer.shipping_details(),
        offer_range={
            "low": "499" if is_india else "34.99",
            "high": "999" if is_india else "39.99",
            "currency": "INR" if is_india else "EUR",
        },
        store_review_count=0,
        store_avg_rating=0,
        lens_prices={},
    )


def _jsonld(html):
    blocks = re.findall(
        r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S)
    parsed = [json.loads(block) for block in blocks]
    return blocks, parsed


def _product_data(html):
    _blocks, parsed = _jsonld(html)
    return next(data for data in parsed if data.get("@type") == "Product")


def _home_data(html):
    _blocks, parsed = _jsonld(html)
    return next(data for data in parsed if data.get("@type") == "CollectionPage")


def _home_product_offers(html):
    data = _home_data(html)
    products = [
        entry["item"]
        for entry in data["mainEntity"]["itemListElement"]
        if entry.get("item", {}).get("@type") == "Product"
    ]
    return [product["offers"] for product in products]


class _Response:
    def __init__(self, body):
        self.body = body
        self.headers = {}

    def get_data(self, as_text=False):
        return self.body if as_text else self.body.encode("utf-8")


FEED_ROWS = [_product(1, "BB44"), _product(2, "AA01")]


class _Cursor:
    def execute(self, *_args, **_kwargs):
        return None

    def fetchall(self):
        return copy.deepcopy(FEED_ROWS)


class _DB:
    def cursor(self):
        return _Cursor()


def _load_feed_view():
    tree = ast.parse(_source("models.py"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "google_merchant_feed"
    )
    function.decorator_list = []
    namespace = {
        "os": os,
        "india_offer": india_offer,
        "request": SimpleNamespace(host="optiwar.in"),
        "_req_is_india": lambda: _FEED_NAMESPACE["request"].host.startswith(
            "optiwar.in"
        ),
        "get_db": lambda: _DB(),
        "ensure_gmc_columns": lambda cursor: None,
        "catalogue_site_filter": lambda: "",
        "display_name": lambda product: product.get("product_name"),
        "RENAMED": set(),
        "slugify": lambda value: value.lower().replace(" ", "-"),
        "is_merchant_eligible": lambda product: (
            product.get("product_status") == "ACTIVE"
            and int(product.get("product_quantity") or 0) > 0
        ),
        "is_contact_lens": lambda product: (
            product.get("product_vertical") == "CONTACT_LENS"
        ),
        "versioned_angle_urls": lambda images, base, limit=11: [
            "%s/static/%s" % (base, image.strip())
            for image in (images or "").split(",")[:limit]
        ],
        "frame_shape": lambda product: "",
        "age_group": lambda product: "",
        "_lens_feed_items": lambda cursor, base, is_india: [],
        "make_response": _Response,
    }
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(module, "models.py", "exec"), namespace)
    return namespace["google_merchant_feed"], namespace


_FEED_VIEW, _FEED_NAMESPACE = _load_feed_view()
XML_NS = {"g": "http://base.google.com/ns/1.0"}


def _render_feed(host, flag):
    _FEED_NAMESPACE["request"] = SimpleNamespace(host=host)
    with patch.dict(os.environ, {india_offer.FLAG: flag}):
        response = _FEED_VIEW()
    return response.get_data(as_text=True)


def _feed_items(xml):
    root = ElementTree.fromstring(xml)
    return root.findall(".//item")


def _render_snippet(source, pattern, **context):
    match = re.search(pattern, source, re.S)
    if not match:
        raise AssertionError("template snippet not found: %s" % pattern)
    return Environment().from_string(match.group(1)).render(**context)


class IndiaShippingDeclaredTests(unittest.TestCase):
    def test_flag_requires_a_trimmed_exact_one(self):
        for value in ("", "true", "01", "yes"):
            with patch.dict(os.environ, {india_offer.FLAG: value}):
                self.assertFalse(india_offer.enabled())
        with patch.dict(os.environ, {india_offer.FLAG: " 1 "}):
            self.assertTrue(india_offer.enabled())

    def test_india_feed_has_one_standard_shipping_tag_per_frame_when_on(self):
        xml = _render_feed("optiwar.in", "1")
        items = _feed_items(xml)
        self.assertEqual(len(items), len(FEED_ROWS))
        for item in items:
            shipping = item.findall("g:shipping", XML_NS)
            self.assertEqual(len(shipping), 1)
            self.assertEqual(shipping[0].findtext("g:country", namespaces=XML_NS), "IN")
            self.assertEqual(
                shipping[0].findtext("g:service", namespaces=XML_NS),
                "Standard",
            )
            self.assertEqual(shipping[0].findtext("g:price", namespaces=XML_NS), "0 INR")

    def test_india_feed_off_and_global_feed_on_or_off_have_no_shipping(self):
        for host, flag in (
            ("optiwar.in", ""),
            ("optiwar.com", "1"),
            ("optiwar.com", ""),
        ):
            with self.subTest(host=host, flag=flag):
                items = _feed_items(_render_feed(host, flag))
                self.assertTrue(items)
                self.assertTrue(all(
                    item.findall("g:shipping", XML_NS) == [] for item in items
                ))

    def test_india_product_home_and_feed_never_emit_return_schema(self):
        for declared, flag in ((False, ""), (True, "1")):
            outputs = (
                _render_product(True, declared),
                _render_home(True, declared),
                _render_feed("optiwar.in", flag),
            )
            for output in outputs:
                with self.subTest(declared=declared):
                    self.assertNotIn("MerchantReturnPolicy", output)
                    self.assertNotIn("merchantReturnDays", output)

    def test_india_offer_values_match_standard_delivery_without_return_policy(self):
        with patch.dict(os.environ, {india_offer.FLAG: "1"}):
            offer = _product_data(_render_product(True, True))["offers"][0]

        details = offer["shippingDetails"]
        self.assertEqual(details, india_offer.shipping_details())
        self.assertEqual(details["shippingRate"], {
            "@type": "MonetaryAmount", "value": "0", "currency": "INR",
        })
        self.assertEqual(details["shippingDestination"], {
            "@type": "DefinedRegion", "addressCountry": "IN",
        })
        self.assertNotIn("deliveryTime", details)
        self.assertNotIn("hasMerchantReturnPolicy", offer)
        self.assertNotIn("merchantReturnDays", offer)

    def test_india_product_offer_keeps_old_shipping_without_return_schema(self):
        offer = _product_data(_render_product(True, False))["offers"][0]
        self.assertEqual(set(offer), {
            "@type", "price", "priceCurrency", "eligibleRegion",
            "availability", "seller", "priceValidUntil", "url",
            "itemCondition", "shippingDetails",
        })
        self.assertEqual(
            {key: value for key, value in offer.items()
             if key != "shippingDetails"},
            {
                "@type": "Offer",
                "price": "601",
                "priceCurrency": "INR",
                "eligibleRegion": {"@type": "Country", "name": "IN"},
                "availability": "https://schema.org/InStock",
                "seller": {"@type": "Organization", "name": "Optiwar"},
                "priceValidUntil": "2027-12-31",
                "url": (
                    "https://optiwar.in/categories/spectacles-frame/"
                    "optiwar-test-frame?pid=1"
                ),
                "itemCondition": "https://schema.org/NewCondition",
            },
        )
        self.assertEqual(offer["shippingDetails"], {
            "@type": "OfferShippingDetails",
            "shippingRate": {
                "@type": "MonetaryAmount", "value": "0", "currency": "INR",
            },
            "shippingDestination": {
                "@type": "DefinedRegion", "addressCountry": "IN",
            },
            "deliveryTime": {
                "@type": "ShippingDeliveryTime",
                "handlingTime": {
                    "@type": "QuantitativeValue",
                    "minValue": 1, "maxValue": 2, "unitCode": "DAY",
                },
                "transitTime": {
                    "@type": "QuantitativeValue",
                    "minValue": 3, "maxValue": 7, "unitCode": "DAY",
                },
            },
        })
        self.assertNotIn("hasMerchantReturnPolicy", offer)
        self.assertNotIn("merchantReturnDays", offer)

    def test_global_product_offer_is_unchanged_with_switch_on_or_off(self):
        off_html = _render_product(False, False)
        on_html = _render_product(False, True)
        off_offer = _product_data(off_html)["offers"][0]
        on_offer = _product_data(on_html)["offers"][0]
        self.assertEqual(off_offer, on_offer)
        self.assertEqual(
            re.search(
                r'<script type="application/ld\+json">(.*?)</script>',
                off_html, re.S,
            ).group(1),
            re.search(
                r'<script type="application/ld\+json">(.*?)</script>',
                on_html, re.S,
            ).group(1),
        )
        self.assertEqual(off_offer["priceCurrency"], "EUR")
        self.assertEqual(
            off_offer["hasMerchantReturnPolicy"], RETURN_POLICY_COM)
        self.assertEqual(off_offer["shippingDetails"]["shippingRate"], {
            "@type": "MonetaryAmount", "value": "0", "currency": "EUR",
        })
        self.assertEqual(
            len(off_offer["shippingDetails"]["shippingDestination"]), 35)
        self.assertEqual(
            off_offer["shippingDetails"]["deliveryTime"]["handlingTime"],
            {"@type": "QuantitativeValue", "minValue": 1, "maxValue": 2,
             "unitCode": "DAY"},
        )
        self.assertEqual(
            off_offer["shippingDetails"]["deliveryTime"]["transitTime"],
            {"@type": "QuantitativeValue", "minValue": 3, "maxValue": 7,
             "unitCode": "DAY"},
        )
        self.assertEqual(
            [row["addressCountry"]
             for row in off_offer["shippingDetails"]["shippingDestination"]],
            RETURN_COUNTRIES_COM,
        )

    def test_india_home_product_offers_are_switch_gated(self):
        with patch.dict(os.environ, {india_offer.FLAG: ""}):
            off_html = _render_home(True, False)
        with patch.dict(os.environ, {india_offer.FLAG: "1"}):
            on_html = _render_home(True, True)
        off_offers = _home_product_offers(off_html)
        on_offers = _home_product_offers(on_html)
        self.assertEqual(len(off_offers), 2)
        self.assertEqual(len(on_offers), 2)
        for offer in off_offers:
            self.assertNotIn("shippingDetails", offer)
            self.assertNotIn("hasMerchantReturnPolicy", offer)
        for offer in on_offers:
            self.assertEqual(offer["shippingDetails"], india_offer.shipping_details())
            self.assertNotIn("hasMerchantReturnPolicy", offer)
            self.assertNotIn("merchantReturnDays", offer)

    def test_all_product_and_home_jsonld_blocks_parse(self):
        for html in (
            _render_product(True, False),
            _render_product(True, True),
            _render_product(False, False),
            _render_product(False, True),
            _render_home(True, False),
            _render_home(True, True),
            _render_home(False, False),
            _render_home(False, True),
        ):
            with self.subTest(page="render"):
                _jsonld(html)

    def test_requested_india_copy_and_global_copy(self):
        product_source = _source("templates/product_page.html")
        price_note = (
            r'<p class="price-note">(.*?)</p>'
        )
        self.assertEqual(
            _render_snippet(
                product_source, price_note, is_india=True, lens=None),
            "Inclusive of all taxes. Standard delivery is free across India.",
        )
        self.assertEqual(
            _render_snippet(
                product_source, price_note, is_india=False, lens=None),
            "Free delivery worldwide.",
        )
        for is_india, declared, expected in (
            (
                True, False,
                "Complete checkout and pay online. Standard delivery is free "
                "across India.",
            ),
            (
                True, True,
                "Complete checkout and pay online. Standard delivery is free "
                "across India.",
            ),
            (
                False, False,
                "Complete checkout and pay online. Your glasses will be "
                "delivered in 5-7 business days with free shipping.",
            ),
        ):
            rendered = _render_product(is_india, declared)
            _blocks, parsed = _jsonld(rendered)
            howto_data = next(data for data in parsed if data.get("@type") == "HowTo")
            self.assertEqual(howto_data["step"][3]["text"], expected)
            if is_india:
                self.assertNotIn("5 days", howto_data["step"][3]["text"])
                self.assertNotIn("within 5", howto_data["step"][3]["text"])

        checkout_source = _source("templates/checkout.html")
        checkout_snippet = (
            r'<p style="font-size:14px;color:#475569;line-height:1\.7;">(.*?)</p>'
        )
        self.assertEqual(
            _render_snippet(checkout_source, checkout_snippet, is_india=True),
            "Standard delivery is free across India. Expedite and 24-hour "
            "Delhi NCR delivery are optional paid upgrades.",
        )
        checkout_text = _render_snippet(
            checkout_source, checkout_snippet, is_india=True)
        self.assertNotIn("5 days", checkout_text)
        self.assertNotIn("within 5", checkout_text)
        self.assertEqual(
            _render_snippet(checkout_source, checkout_snippet, is_india=False),
            "Currently for a limited period we are offering free shipping and "
            "subject to our terms and conditions these may change at a future "
            "date.",
        )

        footer_source = _source("templates/base.html")
        footer_snippet = (
            r'<p class="ow-footer-text" style="margin-top:8px;">(.*?)</p>'
        )
        self.assertEqual(
            _render_snippet(footer_source, footer_snippet, is_india=True),
            "<strong>Standard delivery is free</strong> across India. Prices "
            "include GST.",
        )
        self.assertEqual(
            _render_snippet(footer_source, footer_snippet, is_india=False),
            "<strong>Free shipping</strong> worldwide. All taxes included.",
        )

    def test_home_howto_keeps_global_copy_and_updates_india_copy(self):
        expected = {
            True: (
                "Complete checkout and pay. Standard delivery is free across "
                "India."
            ),
            False: "Complete checkout and pay. Free worldwide delivery in 7-14 business days.",
        }
        for is_india in (True, False):
            html = _render_home(is_india, False)
            _blocks, parsed = _jsonld(html)
            howto = next(data for data in parsed if data.get("@type") == "HowTo")
            self.assertEqual(howto["step"][3]["text"], expected[is_india])
            if is_india:
                self.assertNotIn("5 days", howto["step"][3]["text"])
                self.assertNotIn("within 5", howto["step"][3]["text"])

    def test_existing_delivery_accordion_is_not_on_india_frame_pages(self):
        product_source = _source("templates/product_page.html")
        self.assertIn('<summary>Delivery</summary>', product_source)
        contact_lens = {
            "product_vertical": catalogue.CONTACT_LENS,
            "sell_on_in": 0,
        }
        self.assertFalse(
            catalogue.is_product_allowed(contact_lens, catalogue.SITE_IN))
        self.assertIn(
            'template = "product_page_lens.html" if lens else "product_page.html"',
            _source("models.py"),
        )

    def test_shipping_option_fees_days_and_address_choices_are_unchanged(self):
        self.assertEqual(
            [(option["fee"], option["days"])
             for option in shipping_options.OPTIONS],
            [(0, 5), (300, 1), (1000, 1)],
        )
        now = datetime.datetime(2026, 11, 9, 10, 0)
        india_options = shipping_options.available(
            "Maharashtra", "400001", now=now)
        self.assertEqual(
            [(option["key"], option["fee"]) for option in india_options],
            [("standard", 0), ("expedite", 300)],
        )
        self.assertEqual(
            (shipping_options.choose(
                "expedite", "Maharashtra", "400001", now=now)["key"],
             shipping_options.choose(
                "expedite", "Maharashtra", "400001", now=now)["fee"]),
            ("expedite", 300),
        )
        delhi_options = shipping_options.available(
            "Delhi", "110001", now=now)
        self.assertEqual(
            [(option["key"], option["fee"]) for option in delhi_options],
            [("standard", 0), ("expedite", 300), ("ncr24", 1000)],
        )
        chosen = shipping_options.choose(
            "ncr24", "Delhi", "110001", now=now)
        self.assertEqual((chosen["key"], chosen["fee"]), ("ncr24", 1000))


if __name__ == "__main__":
    unittest.main()
