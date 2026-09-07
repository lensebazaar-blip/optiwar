"""The cart-level lens rule: per-eye minimums, and the eyewear that waives them.

    python3 -m unittest tests.test_lens_cart
"""
import decimal
import importlib.util
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lens_order = _load("lens_order_for_cart", os.path.join(REPO, "lens_order.py"))
lens_cart = _load("lens_cart_under_test", os.path.join(REPO, "lens_cart.py"))

PRECISION1 = {"product_id": 1015, "min_boxes_single_eye": 12,
              "min_boxes_both_per_eye": 6, "special_price_eur": "15.11",
              "price_eur": "26.95", "lens_type": "SPHERICAL"}
PRODUCTS = {"1015": PRECISION1}


def lens(right=0, left=0, pid=1015):
    return {"product_id": str(pid), "product_name": "Precision1",
            "product_category": "Contact Lenses", "vertical": "CONTACT_LENS",
            "right_qty": right, "left_qty": left,
            "order_quantity": right + left,
            "ATC_WCL": 15.11 * (right + left)}


def frame(price, pid=7, qty=1, lenses=0.0):
    return {"product_id": str(pid), "product_name": "Frame %s" % pid,
            "product_category": "Spectacles Frame", "order_quantity": qty,
            "product_special_price": price, "ATC_total": price * qty,
            "server_total_price": lenses}


def sunglasses(price, pid=9):
    return {"product_id": str(pid), "product_category": "Sunglasses",
            "order_quantity": 1, "product_special_price": price,
            "ATC_total": price}


class PerEyeMinimums(unittest.TestCase):
    """LEFT only, RIGHT only, BOTH — against the stated 12 / 6-per-eye."""

    def problems(self, lines, waived=False):
        return lens_order._minimum_problems(PRECISION1, lines, "optiwar.com",
                                            waived)

    def test_left_only_needs_the_single_eye_minimum(self):
        self.assertTrue(self.problems([{"eye": "left", "boxes": 11}]))
        self.assertEqual(self.problems([{"eye": "left", "boxes": 12}]), [])

    def test_right_only_needs_the_single_eye_minimum(self):
        self.assertTrue(self.problems([{"eye": "right", "boxes": 6}]))
        self.assertEqual(self.problems([{"eye": "right", "boxes": 12}]), [])

    def test_both_eyes_need_the_per_eye_minimum_each(self):
        both = [{"eye": "right", "boxes": 6}, {"eye": "left", "boxes": 6}]
        self.assertEqual(self.problems(both), [])
        short = [{"eye": "right", "boxes": 12}, {"eye": "left", "boxes": 5}]
        refused = self.problems(short)
        self.assertEqual(len(refused), 1)
        self.assertIn("left eye", refused[0][1])
        self.assertIn("6 boxes per eye", refused[0][1])

    def test_the_waiver_lowers_the_floor_to_one_box_only(self):
        self.assertEqual(self.problems([{"eye": "left", "boxes": 1}],
                                       waived=True), [])
        self.assertEqual(self.problems([{"eye": "right", "boxes": 1},
                                        {"eye": "left", "boxes": 1}],
                                       waived=True), [])
        self.assertEqual(lens_order.minimums(PRECISION1, "optiwar.com", True),
                         {"single": 1, "both": 1, "waived": True,
                          "stated_single": 12, "stated_both": 6})

    def test_the_page_dictionary_is_unchanged_when_nothing_is_waived(self):
        self.assertEqual(lens_order.minimums(PRECISION1, "optiwar.com"),
                         {"single": 12, "both": 6})

    def test_in_has_no_minimum_and_no_waiver_either(self):
        self.assertEqual(lens_order.minimums(PRECISION1, "optiwar.in", True),
                         {"single": 0, "both": 0})


class EyewearSubtotal(unittest.TestCase):
    def test_only_spectacle_frames_count(self):
        cart = [frame(20.0), sunglasses(100.0), lens(right=1)]
        self.assertEqual(lens_cart.eyewear_subtotal(cart),
                         decimal.Decimal("20"))

    def test_the_frames_lenses_are_part_of_what_the_eyewear_costs(self):
        self.assertEqual(lens_cart.eyewear_subtotal([frame(20.0, lenses=15.0)]),
                         decimal.Decimal("35"))

    def test_the_two_frame_discount_is_subtracted(self):
        cart = [frame(24.0, pid=1), frame(24.0, pid=2)]
        self.assertEqual(lens_cart.eyewear_subtotal(cart),
                         decimal.Decimal("33"))
        self.assertFalse(lens_cart.minimums_waived(cart))

    def test_thirty_five_exactly_waives_and_thirty_four_ninety_nine_does_not(self):
        self.assertTrue(lens_cart.minimums_waived([frame(35.0)]))
        self.assertFalse(lens_cart.minimums_waived([frame(34.99)]))

    def test_a_lens_subtotal_or_cart_total_never_waives(self):
        cart = [lens(right=12, left=12), sunglasses(500.0)]
        self.assertFalse(lens_cart.minimums_waived(cart))

    def test_in_never_waives_whatever_is_in_the_cart(self):
        self.assertFalse(lens_cart.minimums_waived([frame(500.0)],
                                                   site="optiwar.in"))


class Revalidation(unittest.TestCase):
    def test_eyewear_at_or_above_35_keeps_a_single_box(self):
        cart = [frame(36.0), lens(right=1)]
        kept, removed = lens_cart.revalidate(cart, PRODUCTS)
        self.assertEqual(removed, [])
        self.assertEqual(len(kept), 2)

    def test_dropping_below_35_removes_the_lens_below_its_minimum(self):
        # The frame went from €36 to €20: the lens at one box no longer stands.
        cart = [frame(20.0), lens(right=1)]
        kept, removed = lens_cart.revalidate(cart, PRODUCTS)
        self.assertEqual([i["product_id"] for i in removed], ["1015"])
        self.assertEqual([i["product_id"] for i in kept], ["7"])

    def test_removing_the_frame_removes_the_dependent_lens_only(self):
        cart = [lens(right=1), lens(right=6, left=6, pid=1016)]
        products = dict(PRODUCTS, **{"1016": dict(PRECISION1, product_id=1016)})
        kept, removed = lens_cart.revalidate(cart, products)
        self.assertEqual([i["product_id"] for i in removed], ["1015"])
        self.assertEqual([i["product_id"] for i in kept], ["1016"])

    def test_a_lens_that_meets_its_minimum_survives_without_eyewear(self):
        for cart in ([lens(left=12)], [lens(right=12)], [lens(right=6, left=6)]):
            kept, removed = lens_cart.revalidate(cart, PRODUCTS)
            self.assertEqual(removed, [], cart)
            self.assertEqual(len(kept), 1)

    def test_would_remove_is_the_same_check_before_the_change_is_made(self):
        after = [lens(right=1)]
        self.assertEqual([i["product_id"] for i in
                          lens_cart.would_remove(after, PRODUCTS)], ["1015"])

    def test_a_lens_whose_product_is_unknown_is_left_for_checkout(self):
        kept, removed = lens_cart.revalidate([lens(right=1)], {})
        self.assertEqual(removed, [])
        self.assertEqual(len(kept), 1)

    def test_the_customer_is_told_what_went(self):
        text = lens_cart.describe_removed([lens(right=1)])
        self.assertIn("Precision1 (1 box)", text)
        self.assertIn("eyewear order benefit", text)

    def test_the_confirmation_wording_is_the_owners(self):
        self.assertEqual(
            lens_cart.CONFIRM_REMOVAL,
            "Removing this eyewear item will also remove the contact lenses "
            "that were added under the eyewear order benefit.")


class ProductLookup(unittest.TestCase):
    def test_only_lens_lines_are_looked_up(self):
        class Cursor(object):
            statements = []

            def execute(self, sql, params=None):
                self.statements.append((sql, params))

            def fetchall(self):
                return [{"product_id": 1015, "min_boxes_single_eye": 12,
                         "min_boxes_both_per_eye": 6}]

        cursor = Cursor()
        self.assertEqual(lens_cart.load_products(cursor, [frame(20.0)]), {})
        self.assertEqual(cursor.statements, [])
        found = lens_cart.load_products(cursor, [frame(20.0), lens(right=1)])
        self.assertEqual(cursor.statements[0][1], ("1015",))
        self.assertEqual(found["1015"]["min_boxes_single_eye"], 12)


if __name__ == "__main__":
    unittest.main()
