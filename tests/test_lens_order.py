"""Ordering a lens: the matrix decides, the eyes are independent, we price it.

The rules asserted here: a combination the matrix does not hold is refused; the
options offered are nested so a cylinder is only offered where it exists; each
eye carries its own prescription and its own number of boxes; the charge is the
catalogue's EUR box price times the boxes and never the posted one; and a lens
is purchasable while ON_ORDER because frame quantity does not apply to it.

    python3 -m unittest tests.test_lens_order
"""
import importlib.util
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


def _read(relative):
    with open(os.path.join(REPO, relative)) as handle:
        return handle.read()


lens_order = _load("lens_order")

LENS = {
    "product_id": 2001,
    "product_code": "CL-CV-MDT30",
    "product_name": "MyDay Toric",
    "brand": "CooperVision",
    "pack_quantity": 30,
    "product_price_eur": "45.00",
    "product_special_price_eur": "39.90",
    "availability": "IN_STOCK",
    "lead_time_days": None,
}

# A deliberately holed matrix: -4.50 is made in two cylinders, one of which has
# a single axis, and -5.00 is made spherically only. Nothing here may be
# recombined into a pair the manufacturer does not list.
VARIANTS = [
    {"variant_id": 1, "sph": "-4.50", "cyl": "-0.75", "axis": 180,
     "add_power": None, "color_code": "", "color_name": None},
    {"variant_id": 2, "sph": "-4.50", "cyl": "-0.75", "axis": 90,
     "add_power": None, "color_code": "", "color_name": None},
    {"variant_id": 3, "sph": "-4.50", "cyl": "-1.25", "axis": 180,
     "add_power": None, "color_code": "", "color_name": None},
    {"variant_id": 4, "sph": "-5.00", "cyl": None, "axis": None,
     "add_power": None, "color_code": "", "color_name": None},
]


def _form(**kwargs):
    return dict(kwargs)


class Options(unittest.TestCase):

    def setUp(self):
        self.opts = lens_order.options(VARIANTS)

    def test_a_cylinder_is_offered_only_for_the_sphere_that_has_it(self):
        # colour -> base curve -> sphere -> cylinder. Base curve is a parameter
        # like any other and these rows state none, so its key is ''.
        tree = self.opts["tree"][""][""]
        self.assertEqual(sorted(tree.keys()), ["-4.50", "-5.00"])
        self.assertEqual(sorted(tree["-4.50"].keys()), ["-0.75", "-1.25"])
        self.assertEqual(list(tree["-5.00"].keys()), [""])

    def test_an_axis_is_offered_only_for_the_pair_that_has_it(self):
        tree = self.opts["tree"][""][""]
        self.assertEqual(sorted(tree["-4.50"]["-0.75"]["axes"]),
                         ["180", "90"])
        self.assertEqual(tree["-4.50"]["-1.25"]["axes"], ["180"])
        self.assertEqual(tree["-5.00"][""]["axes"], [])


def _rule_lists(**lists):
    return {param: [{"value": v, "label": v} for v in values]
            for param, values in lists.items()}


# MyDay Toric as its source states it: three independent lists and one base
# curve. 53 x 4 x 18 combinations are orderable and none of them is a row.
MYDAY = _rule_lists(
    sph=["%.2f" % (-9.00 + 0.25 * s) for s in range(53)],
    cyl=["-0.75", "-1.25", "-1.75", "-2.25"],
    axis=[str(a) for a in range(10, 181, 10)],
    base_curve=["8.60"], diameter=["14.50"])


class StatedAsRules(unittest.TestCase):
    """A lens whose source states parameters rather than combinations."""

    def setUp(self):
        self.shape = lens_order.selectable(MYDAY, "TORIC")

    def test_the_lists_are_offered_and_no_combinations_are_materialised(self):
        options = self.shape.options()
        self.assertEqual(options["mode"], "RULES")
        self.assertEqual(options["tree"], {})
        self.assertEqual(len(options["lists"]["sph"]), 53)
        self.assertEqual(len(options["lists"]["axis"]), 18)
        self.assertEqual([e["value"] for e in options["lists"]["cyl"]],
                         ["-0.75", "-1.25", "-1.75", "-2.25"])

    def test_a_combination_of_stated_values_is_accepted(self):
        sel = lens_order.read_eye(
            _form(right_sph="-4.50", right_cyl="-1.75", right_axis="70",
                  right_boxes="1"), "right")
        lines, errors = lens_order.validate(MYDAY, LENS, [sel],
                                            lens_type="TORIC")
        self.assertEqual(errors, [])
        self.assertEqual(lines[0]["variant"]["cyl"], "-1.75")
        # There is no row, so there is no row id: the selection is the record.
        self.assertIsNone(lines[0]["variant"]["variant_id"])

    def test_a_value_the_source_never_stated_is_refused(self):
        for bad in (_form(right_sph="-9.25", right_cyl="-1.75",
                          right_axis="70", right_boxes="1"),
                    _form(right_sph="-4.50", right_cyl="-3.00",
                          right_axis="70", right_boxes="1"),
                    _form(right_sph="-4.50", right_cyl="-1.75",
                          right_axis="75", right_boxes="1")):
            sel = lens_order.read_eye(bad, "right")
            _lines, errors = lens_order.validate(MYDAY, LENS, [sel],
                                                 lens_type="TORIC")
            self.assertTrue(errors, bad)

    def test_a_toric_ordered_without_its_cylinder_or_axis_is_refused(self):
        sel = lens_order.read_eye(_form(right_sph="-4.50", right_boxes="1"),
                                  "right")
        _lines, errors = lens_order.validate(MYDAY, LENS, [sel],
                                             lens_type="TORIC")
        self.assertTrue(errors)

    def test_a_parameter_this_lens_is_not_chosen_on_is_refused(self):
        sel = lens_order.read_eye(
            _form(right_sph="-4.50", right_cyl="-1.75", right_axis="70",
                  right_add="2.00", right_boxes="1"), "right")
        _lines, errors = lens_order.validate(MYDAY, LENS, [sel],
                                             lens_type="TORIC")
        self.assertTrue(errors)

    def test_the_one_base_curve_is_filled_in_rather_than_asked(self):
        sel = lens_order.read_eye(
            _form(right_sph="-4.50", right_cyl="-1.75", right_axis="70",
                  right_boxes="1"), "right")
        lines, errors = lens_order.validate(MYDAY, LENS, [sel],
                                            lens_type="TORIC")
        self.assertEqual(errors, [])
        self.assertEqual(lines[0]["variant"]["base_curve"], "8.60")
        self.assertEqual(lines[0]["variant"]["diameter"], "14.50")

    def test_a_second_diameter_is_refused_rather_than_chosen_for_the_customer(self):
        # Nobody is asked for a diameter, so picking the first stated one would
        # ship a lens the customer never chose. The importer refuses this too.
        two = dict(MYDAY, diameter=[{"value": "14.50", "label": "14.5"},
                                    {"value": "14.20", "label": "14.2"}])
        sel = lens_order.read_eye(
            _form(right_sph="-4.50", right_cyl="-1.75", right_axis="70",
                  right_boxes="1"), "right")
        _lines, errors = lens_order.validate(two, LENS, [sel],
                                             lens_type="TORIC")
        self.assertTrue(errors)

    def test_a_second_base_curve_becomes_a_choice_that_must_be_made(self):
        two = dict(MYDAY, base_curve=[{"value": "8.60", "label": "8.6"},
                                      {"value": "9.00", "label": "9.0"}])
        sel = lens_order.read_eye(
            _form(right_sph="-4.50", right_cyl="-1.75", right_axis="70",
                  right_boxes="1"), "right")
        _lines, errors = lens_order.validate(two, LENS, [sel],
                                             lens_type="TORIC")
        self.assertTrue(errors)
        chosen = lens_order.read_eye(
            _form(right_bc="9.00", right_sph="-4.50", right_cyl="-1.75",
                  right_axis="70", right_boxes="1"), "right")
        lines, errors = lens_order.validate(two, LENS, [chosen],
                                            lens_type="TORIC")
        self.assertEqual(errors, [])
        self.assertEqual(lines[0]["variant"]["base_curve"], "9.00")


class MinimumBoxes(unittest.TestCase):
    """The supply terms are the product's own, and the storefront's."""

    def setUp(self):
        self.lens = dict(LENS, min_boxes_single_eye=8,
                         min_boxes_both_per_eye=4)

    def _order(self, lens, site, **boxes):
        sels = [lens_order.read_eye(
            _form(**{"%s_sph" % eye: "-5.00",
                     "%s_boxes" % eye: str(count)}), eye)
            for eye, count in boxes.items()]
        return lens_order.validate_detailed(VARIANTS, lens, sels, site=site)

    def test_one_eye_below_the_single_eye_minimum_is_refused(self):
        _lines, problems = self._order(self.lens, "optiwar.com", right=4)
        self.assertEqual([c for c, _ in problems],
                         [lens_order.REFUSED_MINIMUM])
        self.assertIn("minimum of 8 boxes", problems[0][1])

    def test_the_both_eyes_minimum_is_per_eye_and_not_a_total(self):
        # Four per eye: three and five is eight boxes and still refused.
        _lines, problems = self._order(self.lens, "optiwar.com",
                                       right=3, left=5)
        self.assertEqual([c for c, _ in problems],
                         [lens_order.REFUSED_MINIMUM])
        lines, problems = self._order(self.lens, "optiwar.com",
                                      right=4, left=4)
        self.assertEqual(problems, [])
        self.assertEqual(sum(ln["boxes"] for ln in lines), 8)

    def test_india_enforces_no_minimum_while_it_sells_no_lens(self):
        lines, problems = self._order(self.lens, "in.optiwar.com", right=1)
        self.assertEqual(problems, [])
        self.assertEqual(lines[0]["boxes"], 1)

    def test_a_lens_with_no_stated_minimum_sells_from_one_box(self):
        lines, problems = self._order(LENS, "optiwar.com", right=1)
        self.assertEqual(problems, [])
        self.assertEqual(lines[0]["boxes"], 1)

    def test_the_page_is_told_the_minimums_it_must_show(self):
        self.assertEqual(lens_order.minimums(self.lens, "optiwar.com"),
                         {"single": 8, "both": 4})
        self.assertEqual(lens_order.minimums(self.lens, "in.optiwar.com"),
                         {"single": 0, "both": 0})


class Selection(unittest.TestCase):

    def test_a_listed_combination_is_accepted(self):
        sel = lens_order.read_eye(
            _form(right_sph="-4.50", right_cyl="-1.25", right_axis="180",
                  right_boxes="2"), "right")
        lines, errors = lens_order.validate(VARIANTS, LENS, [sel])
        self.assertEqual(errors, [])
        self.assertEqual(lines[0]["variant"]["variant_id"], 3)
        self.assertEqual(lines[0]["boxes"], 2)

    def test_a_combination_the_matrix_does_not_hold_is_refused(self):
        # -4.50/-1.25 exists and axis 90 exists, but not together.
        sel = lens_order.read_eye(
            _form(right_sph="-4.50", right_cyl="-1.25", right_axis="90",
                  right_boxes="1"), "right")
        lines, errors = lens_order.validate(VARIANTS, LENS, [sel])
        self.assertEqual(lines, [])
        self.assertIn("not made for this lens", errors[0])

    def test_a_cylinder_on_a_spherical_row_is_refused(self):
        sel = lens_order.read_eye(
            _form(right_sph="-5.00", right_cyl="-0.75", right_boxes="1"),
            "right")
        _lines, errors = lens_order.validate(VARIANTS, LENS, [sel])
        self.assertTrue(errors)

    def test_a_power_between_two_listed_steps_is_refused(self):
        sel = lens_order.read_eye(
            _form(right_sph="-4.75", right_boxes="1"), "right")
        _lines, errors = lens_order.validate(VARIANTS, LENS, [sel])
        self.assertTrue(errors)

    def test_the_form_and_the_database_agree_on_a_number(self):
        # The form posts -4.5, the row holds Decimal('-4.50').
        sel = lens_order.read_eye(
            _form(right_sph="-4.5", right_cyl="-1.25", right_axis="180",
                  right_boxes="1"), "right")
        lines, errors = lens_order.validate(VARIANTS, LENS, [sel])
        self.assertEqual(errors, [])
        self.assertEqual(lines[0]["variant"]["variant_id"], 3)

    def test_an_order_for_no_eye_is_refused(self):
        sels = [lens_order.read_eye(_form(), eye) for eye in lens_order.EYES]
        _lines, errors = lens_order.validate(VARIANTS, LENS, sels)
        self.assertEqual(errors, ["Choose the boxes for at least one eye"])

    def test_an_absurd_number_of_boxes_is_refused(self):
        sel = lens_order.read_eye(
            _form(right_sph="-5.00", right_boxes="500"), "right")
        _lines, errors = lens_order.validate(VARIANTS, LENS, [sel])
        self.assertIn("at most", errors[0])

    def test_a_refusal_carries_a_reason_code_for_the_event_stream(self):
        """The customer gets the sentence; the report counts the code."""
        sel = lens_order.read_eye(
            _form(right_sph="-4.50", right_cyl="-1.25", right_axis="90",
                  right_boxes="1"), "right")
        _lines, problems = lens_order.validate_detailed(VARIANTS, LENS, [sel])
        self.assertEqual([c for c, _ in problems],
                         [lens_order.REFUSED_NOT_MADE])
        self.assertIn("not made for this lens", problems[0][1])

    def test_no_boxes_and_no_price_have_their_own_codes(self):
        empty = lens_order.read_eye(_form(), "right")
        _l, problems = lens_order.validate_detailed(VARIANTS, LENS, [empty])
        self.assertEqual([c for c, _ in problems],
                         [lens_order.REFUSED_NO_BOXES])
        priced = lens_order.read_eye(
            _form(right_sph="-5.00", right_boxes="1"), "right")
        free = dict(LENS, product_price_eur=0,
                    product_special_price_eur=None)
        _l, problems = lens_order.validate_detailed(VARIANTS, free, [priced])
        self.assertEqual([c for c, _ in problems],
                         [lens_order.REFUSED_NO_PRICE])


class PerEyePricing(unittest.TestCase):

    def setUp(self):
        sels = [
            lens_order.read_eye(
                _form(right_sph="-4.50", right_cyl="-1.25", right_axis="180",
                      right_boxes="2"), "right"),
            lens_order.read_eye(
                _form(left_sph="-5.00", left_boxes="3"), "left"),
        ]
        lines, errors = lens_order.validate(VARIANTS, LENS, sels)
        self.assertEqual(errors, [])
        self.item = lens_order.cart_item(LENS, lines)

    def test_the_two_eyes_keep_their_own_prescriptions(self):
        self.assertEqual(self.item["right_pwr"], "-4.50")
        self.assertEqual(self.item["right_cyl"], "-1.25")
        self.assertEqual(self.item["right_axis"], "180")
        self.assertEqual(self.item["left_pwr"], "-5.00")
        self.assertEqual(self.item["left_cyl"], "")
        self.assertEqual(self.item["left_axis"], "")

    def test_five_boxes_are_charged_at_the_box_price(self):
        self.assertEqual(self.item["right_qty"], 2)
        self.assertEqual(self.item["left_qty"], 3)
        self.assertEqual(self.item["order_quantity"], 5)
        self.assertEqual(self.item["product_special_price"], 39.90)
        self.assertEqual(self.item["ATC_WCL"], round(39.90 * 5, 2))

    def test_one_eye_only_leaves_the_other_unset_rather_than_zero_priced(self):
        lines, _errors = lens_order.validate(VARIANTS, LENS, [
            lens_order.read_eye(_form(left_sph="-5.00", left_boxes="1"),
                                "left")])
        item = lens_order.cart_item(LENS, lines)
        self.assertEqual(item["right_qty"], 0)
        self.assertEqual(item["right_eye"], "No RX selected")
        self.assertEqual(item["order_quantity"], 1)
        self.assertEqual(item["ATC_WCL"], 39.90)

    def test_the_line_reads_as_a_prescription(self):
        self.assertEqual(self.item["right_eye"],
                         "SPH -4.50 / CYL -1.25 / AXIS 180 / 2 boxes")
        self.assertEqual(self.item["left_eye"], "SPH -5.00 / 3 boxes")

    def test_the_cart_line_is_the_shape_checkout_already_totals(self):
        # checkout.html and the order writers total
        # ATC_total + server_total_price + ATC_WCL.
        for field in ("product_id", "product_code", "product_name",
                      "product_category", "order_quantity", "ATC_WCL",
                      "right_qty", "left_qty", "rx_id"):
            self.assertIn(field, self.item)


class PriceComesFromTheCatalogue(unittest.TestCase):

    def test_the_offer_price_wins_over_the_list_price(self):
        self.assertEqual(lens_order.box_price(LENS), 39.90)

    def test_the_list_price_is_used_when_there_is_no_offer(self):
        self.assertEqual(
            lens_order.box_price(dict(LENS, product_special_price_eur=None)),
            45.00)

    def test_a_priceless_lens_cannot_be_ordered(self):
        lens = dict(LENS, product_special_price_eur=0, product_price_eur=0)
        sel = lens_order.read_eye(_form(right_sph="-5.00", right_boxes="1"),
                                  "right")
        _lines, errors = lens_order.validate(VARIANTS, lens, [sel])
        self.assertEqual(errors, ["This lens has no price"])


class Availability(unittest.TestCase):

    def test_an_on_order_lens_is_still_purchasable(self):
        lens = dict(LENS, availability="ON_ORDER", lead_time_days=10)
        sel = lens_order.read_eye(_form(right_sph="-5.00", right_boxes="1"),
                                  "right")
        lines, errors = lens_order.validate(VARIANTS, lens, [sel])
        self.assertEqual(errors, [])
        item = lens_order.cart_item(lens, lines)
        self.assertEqual(item["availability"], "ON_ORDER")
        self.assertEqual(item["lead_time_days"], 10)

    def test_frame_quantity_is_not_consulted_anywhere(self):
        # Named in the module docstring as the thing that does not apply; the
        # code below it must never read it.
        self.assertNotIn("product_quantity",
                         _read("lens_order.py").split('"""', 2)[2])


class Wiring(unittest.TestCase):
    """The routes and the template, read as source: the lens must be reachable."""

    def setUp(self):
        self.src = _read("models.py")
        self.pdp = _read(os.path.join("templates", "product_page.html"))

    def test_the_product_page_offers_the_prescription_path_for_a_lens(self):
        # The frame CTA reads product_quantity and a `category` variable the
        # route never passes; a lens branches before both, and buys through
        # the per-eye block that posts to the validated cart route.
        cta = self.pdp.split('<div class="pdp-cta">')[1]
        self.assertTrue(cta.lstrip().startswith("{% if lens %}"))
        self.assertIn("{% include '_lens_eye_cards.html' %}", self.pdp)
        cards = _read(os.path.join("templates", "_lens_eye_cards.html"))
        self.assertIn("url_for('main.lens_add_to_cart')", cards)
        self.assertIn("lens=lens", self.src)
        self.assertIn("_lens_selection_context(", self.src.split(
            "def product_page(")[1].split("\n@bp.route")[0])

    def test_the_eye_cards_offer_only_what_the_lens_states(self):
        # Nothing on the client expands a range: every power comes out of the
        # serialised matrix, and no price is written into the script.
        cards = _read(os.path.join("templates", "_lens_eye_cards.html"))
        script = cards.split("<script>")[1]
        self.assertNotIn("toFixed(2) + ' D'", script)
        self.assertNotRegex(script, r"p\s*-=\s*0\.25")
        self.assertNotRegex(script, r"boxPrice\s*=\s*\d")
        self.assertIn("{{ box_price }}", cards)
        self.assertIn("owLensMatrix", cards)

    def test_the_release_gate_admits_the_selection_and_the_cart(self):
        for route in ("/contact-lenses/select", "/contact-lenses/add"):
            self.assertIn("@bp.route('%s'" % route, self.src)
        for name in ("def lens_select(", "def lens_add_to_cart("):
            body = self.src.split(name)[1].split("\n@bp.route")[0]
            self.assertIn("current_site() == SITE_IN", body)
            self.assertIn("_released_or_previewed_lens(", body)

    def test_the_cart_is_priced_from_the_row_and_not_from_the_form(self):
        body = self.src.split("def lens_add_to_cart(")[1].split(
            "\n@bp.route")[0]
        self.assertIn("lens_order.validate_detailed(", body)
        self.assertIn("lens_order.cart_item(lens, lines)", body)
        for posted in ("product_price", "product_special_price"):
            self.assertNotIn("request.form.get('%s'" % posted, body)

    def test_both_buttons_take_the_one_validated_road(self):
        # "Add to cart" and "Fast checkout" differ only in where the customer
        # lands after the line is validated and priced; neither has a route.
        body = self.src.split("def lens_add_to_cart(")[1].split(
            "\n@bp.route")[0]
        self.assertEqual(body.count("lens_order.validate_detailed("), 1)
        after = body.split("lens_order.cart_item(lens, lines)")[1]
        self.assertIn("request.form.get('intent') == 'cart'", after)
        self.assertIn("lens_seo.lens_path(lens)", after)
        self.assertIn("url_for('main.checkout')", after)
        self.assertNotIn("intent", body.split("lens_order.cart_item")[0])

    def test_a_lens_renders_its_own_page(self):
        body = self.src.split("def product_page(")[1].split("\n@bp.route")[0]
        self.assertIn('"product_page_lens.html" if lens else '
                      '"product_page.html"', body)

    def test_the_selection_template_ships_with_the_route(self):
        deploy = _read(os.path.join("deploy", "deploy.py"))
        for path in ("lens_order.py", "templates/lens_select.html",
                     "templates/product_page_lens.html",
                     "templates/_product_reviews.html",
                     "templates/_lens_eye_cards.html"):
            self.assertIn('"%s"' % path, deploy)


class Render(unittest.TestCase):
    """The selection page renders, so a Jinja defect fails here, not as a 500."""

    def _env(self):
        env = Environment(
            loader=ChoiceLoader([
                DictLoader({"base.html": (
                    "{% block robots %}{% endblock %}"
                    "{% block canonical_url %}{% endblock %}"
                    "{% block hreflang_in %}{% endblock %}"
                    "{% block hreflang_default %}{% endblock %}"
                    "{% block title %}{% endblock %}"
                    "{% block meta_description %}{% endblock %}"
                    "{% block content %}{% endblock %}")}),
                FileSystemLoader(os.path.join(REPO, "templates")),
            ]),
            autoescape=select_autoescape(["html"]))
        env.globals["url_for"] = lambda name, **kw: "/" + name
        env.globals["_"] = lambda s: s
        return env

    def _selection(self, source=VARIANTS, lens_type=None, **extra):
        context = dict(
            lens=LENS, options=lens_order.options(source, lens_type),
            fixed=lens_order.fixed_choices(source, lens_type),
            box_price=lens_order.box_price(LENS), errors=[], submitted={},
            eyes=lens_order.EYES, max_boxes=lens_order.MAX_BOXES_PER_EYE,
            minimums={"single": 1, "both": 1})
        context.update(extra)
        return context

    def _product_page(self, product, passport, **selection):
        env = self._env()
        env.globals["img_has_derivatives"] = lambda path: False
        env.globals["img_ver"] = lambda path, **kw: path
        env.globals["image_dimensions"] = lambda path: (600, 600)
        env.globals["versioned_image_url"] = lambda path, **kw: path
        return env.get_template("product_page_lens.html").render(
            product=product, is_india=False, site_url="https://optiwar.com",
            lens_passport=passport, lens_previewing=True,
            lens_jsonld=[], reviews=[], avg_rating=0, review_count=0,
            request=None, session={}, config={},
            **self._selection(lens=product, **selection))

    def _assert_eye_cards(self, html):
        for field in ("right_sph", "right_cyl", "right_axis", "right_boxes",
                      "left_sph", "left_boxes"):
            self.assertIn('name="%s"' % field, html)
        self.assertIn("39.9", html)
        # The matrix travels as data, so the dependent choices cannot be
        # recombined into a pair that is not listed.
        self.assertIn("-4.50", html)
        self.assertIn("owLensMatrix", html)
        self.assertIn("/main.lens_add_to_cart", html)
        # Two cards, always both on the page, each with its own include box.
        self.assertIn('data-eye="right"', html)
        self.assertIn('data-eye="left"', html)
        self.assertIn('name="right_include"', html)
        self.assertIn('name="left_include"', html)
        self.assertNotIn("ow-rx-which", html)
        self.assertNotIn("data-which", html)
        # Two ways out, both through the same validated route - and both
        # again in the mobile bar.
        self.assertIn('name="intent" value="cart"', html)
        self.assertIn('name="intent" value="checkout"', html)
        bar = html[html.index('id="owLensBar"'):]
        bar = bar[:bar.index("</div>\n\n")]
        self.assertIn('form="owLensForm" name="intent" value="cart"', bar)
        self.assertIn('form="owLensForm" name="intent" value="checkout"', bar)

    def test_the_page_offers_both_eyes_and_the_matrix(self):
        html = self._env().get_template("lens_select.html").render(
            **self._selection())
        self.assertIn("noindex", html)
        self._assert_eye_cards(html)

    def test_a_lens_without_a_stated_minimum_is_not_a_form(self):
        # The stepper starts at the row's minimum. With none stated the page
        # must not choose one for the customer; it says the lens is not ready.
        html = self._env().get_template("lens_select.html").render(
            **self._selection(minimums={"single": 0, "both": 0}))
        self.assertIn('data-role="blocked"', html)
        self.assertNotIn("owLensForm", html)
        self.assertNotIn('name="right_boxes"', html)

    def test_the_boxes_start_at_the_stated_minimum(self):
        html = self._env().get_template("lens_select.html").render(
            **self._selection(minimums={"single": 3, "both": 2}))
        self.assertIn('data-single="3" data-both="2"', html)
        self.assertEqual(html.count('data-role="boxes" inputmode="numeric"\n'
                                    '                 value="2"'), 2)
        self.assertIn("Minimum 3 boxes for one eye, or 2", html)

    def test_a_parameter_made_in_one_value_is_stated_not_asked(self):
        # MyDay states one base curve and one diameter. The card shows them
        # as facts and submits the curve; only powers become a selector.
        html = self._env().get_template("lens_select.html").render(
            **self._selection(source=MYDAY, lens_type="TORIC"))
        self.assertIn('type="hidden" name="right_bc" value="8.60"', html)
        self.assertIn('type="hidden" name="left_bc" value="8.60"', html)
        self.assertNotIn('<select name="right_bc"', html)
        self.assertIn("BC 8.6 mm", html)
        self.assertIn("DIA 14.5 mm", html)
        self.assertIn('<select name="right_sph"', html)
        self.assertIn('<select name="right_cyl"', html)
        # A parameter this lens is not configured on is not a question.
        self.assertNotIn('<select name="right_add"', html)

    def test_a_parameter_made_in_several_values_is_a_selector(self):
        source = dict(MYDAY, **_rule_lists(base_curve=["8.40", "8.60"]))
        html = self._env().get_template("lens_select.html").render(
            **self._selection(source=source, lens_type="TORIC"))
        self.assertIn('<select name="right_bc"', html)
        self.assertIn('value="8.40"', html)
        self.assertNotIn('type="hidden" name="right_bc"', html)

    def _precision1(self):
        product = dict(
            LENS, product_quantity=None, product_category="Contact Lenses",
            product_code="CL-PRECISION1", product_name="Precision1 Daily "
            "Disposable Contact Lenses - 30 Pack", brand="Precision1",
            product_slug="precision1-daily-disposable-contact-lenses--30-pack",
            product_price_eur=26.95, product_special_price_eur=15.11,
            product_price=2479, product_special_price=1390,
            material="verofilcon A", manufacturer="Alcon",
            modality="DAILY", lens_type="SPHERICAL", replacement_days=1,
            water_content=None, gtin=None, product_details="")
        passport = {"images": [
            {"path": "./catalog/contact-lenses/PRECISION1/01_hero.jpg",
             "alt": "Precision1 carton, front"},
            {"path": "./catalog/contact-lenses/PRECISION1/03_side.jpg",
             "alt": "Precision1 carton, side"}],
            "ordering": {"matrix": {
                "base_curve": {"min": 8.3, "max": 8.3},
                "diameter": {"min": 14.2, "max": 14.2},
                "sph": {"min": -12.0, "max": -0.5}}}}
        source = _rule_lists(sph=["-0.50", "-1.00", "-12.00"],
                             base_curve=["8.30"], diameter=["14.20"])
        return product, passport, source

    def test_the_lens_page_is_the_lens_and_nothing_of_a_frame(self):
        product, passport, source = self._precision1()
        html = self._product_page(product, passport, source=source,
                                  lens_type="SPHERICAL")
        body = html.split("<style>")[0] + html.split("</style>", 1)[1]
        # Identity: maker, the commercial name, modality - and the SKU is not
        # in the title. It is stated once, in the specifications.
        self.assertIn('<p class="lpdp-maker">Alcon</p>', html)
        self.assertIn('<h1 class="lpdp-title">Precision1 Daily Disposable '
                      'Contact Lenses - 30 Pack</h1>', html)
        self.assertIn("Daily disposable", html)
        self.assertIn("30 lenses/box", html)
        self.assertEqual(body.count("CL-PRECISION1"),
                         body.count('<td class="val">CL-PRECISION1</td>'))
        # Price: sale per box, list struck through, the real percentage.
        self.assertIn("&euro;15.11 <small>/ box</small>", html)
        self.assertIn('<span class="lpdp-price-was">&euro;26.95</span>', html)
        self.assertIn("44% OFF", html)
        # The facts are stated once, in the cards and the specifications;
        # there is no second spec block between the buy action and details.
        self.assertNotIn("Lens Intelligence", html)
        self.assertNotIn("lpdp-intel", html)
        for fact in ("BC 8.3 mm", "DIA 14.2 mm", "verofilcon A"):
            self.assertIn(fact, html)
        # BC/DIA are lens-level: one fact line above both cards, plus the
        # Specifications row -- not once per eye.
        self.assertEqual(html.count('class="ow-rx-facts"'), 1)
        self.assertEqual(html.count("BC 8.3 mm"), 1)
        self.assertIn('<td class="lbl">Base curve</td><td class="val">8.3 mm</td>', html)
        # Per-eye totals live only in the Order Summary.
        self.assertNotIn("ow-rx-line", html)
        self.assertNotIn('data-role="line"', html)
        # BC is submitted, not asked.
        self.assertIn('type="hidden" name="right_bc" value="8.30"', html)
        self.assertNotIn('<select name="right_bc"', html)
        # The six sections, and both photographs.
        for section in ("Description", "Specifications",
                        "Ordering &amp; Prescription", "Shipping &amp; Returns",
                        "Manufacturer Information", "FAQs"):
            self.assertIn("<summary>%s</summary>" % section, html)
        self.assertIn("01_hero.jpg", html)
        self.assertIn("03_side.jpg", html)
        self.assertIn('id="pdpMain"', html)
        # Nothing of a frame, nothing invented.
        for absent in ("Measure your Face", "Face Match", "About This Frame",
                       "About this frame", "Complimentary", "In Stock</div>",
                       "Factory Outlet Price", "S / M / L", "owFilterWidget",
                       "Water content", "51%", "1,204", "30-day",
                       "Best price", "Vision-tested"):
            self.assertNotIn(absent, html)
        self.assertNotIn("$", body)
        # A spherical lens asks for a power per eye and nothing toric.
        for field in ("right_sph", "right_boxes", "left_sph", "left_boxes"):
            self.assertIn('name="%s"' % field, html)
        self.assertNotIn('name="right_cyl"', html)
        self.assertNotIn('name="right_axis"', html)
        self.assertIn('name="right_include"', html)
        self.assertIn('name="left_include"', html)
        self.assertNotIn("data-which", html)
        self.assertIn('name="intent" value="cart"', html)
        self.assertIn('name="intent" value="checkout"', html)
        self.assertIn("/main.lens_add_to_cart", html)

    def test_the_lens_page_renders_the_owner_preview_as_noindex(self):
        product, passport, source = self._precision1()
        html = self._product_page(product, passport, source=source,
                                  lens_type="SPHERICAL")
        self.assertIn("noindex", html)
        self.assertIn("Owner preview", html)


if __name__ == "__main__":
    unittest.main()
