"""Regression tests for GMC / structured-data currency isolation (P1-22 Batch 1).

Guards the cross-surface correctness fix:

  - optiwar.in must emit an INR-only Product Offer in JSON-LD (the stray
    "Global" EUR Offer that used to be appended on the India storefront is
    gone);
  - optiwar.com must emit a EUR-only Product Offer;
  - each storefront exposes exactly ONE Product Offer currency.

Renders the real templates/product_page.html `jsonld_extra` block in isolation
via Jinja2 (stub base.html + _picture.html), so it runs without the full Flask
app or a database.

    python3 -m unittest tests.test_gmc_currency_isolation
"""
import json
import os
import re
import unittest

from jinja2 import Environment, DictLoader, select_autoescape

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _product():
    return {
        'product_id': 1,
        'product_code': 'BB44',
        'product_name': 'OPTIWAR BABY FRAME',
        'product_details': 'OPTIWAR BABY FRAME BB44 in Blue.',
        'product_category': 'Spectacles Frame',
        'product_slug': 'optiwar-baby-frame',
        'product_image': 'BB44/BB44_1.jpg,BB44/BB44_2.jpg',
        'product_quantity': 5,
        'product_price': 2099,
        'product_special_price': 601,
        'product_price_eur': '50.99',
        'product_special_price_eur': '36.01',
        'color_display': 'Blue',
        'product_color': 'Blue',
        'product_size': '43-17-130',
        'product_material': 'Acetate',
        'product_perception_value': 'Small',
        'product_primary_color': 'Blue',
        'product_secondary_color': 'Blue',
        'product_diameter': '43',
        'product_bridge': '17',
        'product_lenght': '130',
    }


def _render(is_india):
    with open(os.path.join(REPO, 'templates', 'product_page.html'),
              encoding='utf-8') as fh:
        product_page = fh.read()
    env = Environment(
        loader=DictLoader({
            'base.html': '{% block jsonld_extra %}{% endblock %}',
            '_picture.html': (
                '{% macro product_picture() %}{% endmacro %}'
                '{% macro thumb_img() %}{% endmacro %}'
            ),
            'product_page.html': product_page,
        }),
        autoescape=select_autoescape(['html']),
    )
    env.globals.update(
        versioned_image_url=lambda img, url='': '%s/static/%s' % (url, img),
        image_dimensions=lambda img: (50, 20),
        frame_shape=lambda p: 'Rectangle',
    )
    env.filters['_'] = lambda s: s
    env.globals['_'] = lambda s: s
    return env.get_template('product_page.html').render(
        product=_product(),
        is_india=is_india,
        site_url='https://optiwar.in' if is_india else 'https://optiwar.com',
        review_count=0,
        reviews=[],
        avg_rating=0,
        inr_disc_pct=71,
        eur_disc_pct=29,
    )


def _product_jsonld(html):
    """Return the parsed first application/ld+json block (the Product)."""
    blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    for b in blocks:
        data = json.loads(b)
        if data.get('@type') == 'Product':
            return data
    raise AssertionError('no Product JSON-LD block found')


INTERNATIONAL_COUNTRIES = [
    "AU", "AT", "BH", "BE", "CA", "CY", "DK", "FI", "FR", "DE", "GR", "HU",
    "IE", "IL", "IT", "JP", "KW", "MU", "NL", "NZ", "NO", "OM", "PA", "PL",
    "PT", "RO", "SA", "SG", "ZA", "ES", "SE", "CH", "AE", "GB", "QA",
]


class CurrencyIsolationTest(unittest.TestCase):
    def _currencies(self, product):
        offers = product['offers']
        if isinstance(offers, dict):
            offers = [offers]
        return [o.get('priceCurrency') for o in offers]

    def test_india_is_inr_only(self):
        product = _product_jsonld(_render(is_india=True))
        currencies = self._currencies(product)
        self.assertEqual(
            currencies, ['INR'],
            'optiwar.in must expose exactly one INR Offer, got %r' % currencies)
        self.assertNotIn('EUR', currencies, 'stray EUR Offer on optiwar.in')

    def test_global_is_eur_only(self):
        product = _product_jsonld(_render(is_india=False))
        currencies = self._currencies(product)
        self.assertEqual(
            currencies, ['EUR'],
            'optiwar.com must expose exactly one EUR Offer, got %r' % currencies)
        self.assertNotIn('INR', currencies, 'stray INR Offer on optiwar.com')

    def test_india_offer_retains_its_existing_shipping_without_return_data(self):
        offer = _product_jsonld(_render(is_india=True))['offers'][0]
        self.assertEqual(offer['shippingDetails'], {
            '@type': 'OfferShippingDetails',
            'shippingRate': {
                '@type': 'MonetaryAmount', 'value': '0', 'currency': 'INR'},
            'shippingDestination': {
                '@type': 'DefinedRegion', 'addressCountry': 'IN'},
            'deliveryTime': {
                '@type': 'ShippingDeliveryTime',
                'handlingTime': {
                    '@type': 'QuantitativeValue', 'minValue': 1,
                    'maxValue': 2, 'unitCode': 'DAY'},
                'transitTime': {
                    '@type': 'QuantitativeValue', 'minValue': 3,
                    'maxValue': 7, 'unitCode': 'DAY'},
            },
        })
        self.assertNotIn('hasMerchantReturnPolicy', offer)

    def test_com_offer_matches_its_non_returnable_terms_without_timing(self):
        offer = _product_jsonld(_render(is_india=False))['offers'][0]
        shipping = offer['shippingDetails']
        returns = offer['hasMerchantReturnPolicy']
        self.assertEqual(shipping['shippingRate'], {
            '@type': 'MonetaryAmount', 'value': '0', 'currency': 'EUR'})
        self.assertEqual(returns['returnPolicyCategory'],
                         'https://schema.org/MerchantReturnNotPermitted')
        self.assertEqual(returns['applicableCountry'], INTERNATIONAL_COUNTRIES)
        self.assertEqual(shipping['shippingDestination'], [
            {'@type': 'DefinedRegion', 'addressCountry': country}
            for country in INTERNATIONAL_COUNTRIES])
        self.assertEqual(len(returns['applicableCountry']), 35)
        self.assertNotIn('deliveryTime', shipping)
        for key in ('merchantReturnDays', 'returnMethod', 'returnFees',
                    'description'):
            self.assertNotIn(key, returns)


if __name__ == '__main__':
    unittest.main()
