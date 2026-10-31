"""AI Glasses (ai_glasses.py): prescription rules, prices, dispatch and visibility.

No database: the rules are pure functions over a product row.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ai_glasses_under_test",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_glasses.py"))
aig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aig)

PRODUCT = {"rx_allowed": 1, "rx_total_min": -12.0, "rx_total_max": 6.0,
           "product_price": 39900, "product_special_price": 29925, "sales_status": "PREORDER"}
SINGLE = aig.LENS_BY_KEY["single"]
PROG = aig.LENS_BY_KEY["progressive"]


def rx(**kw):
    form = {"right_sph": "-2.00", "right_cyl": "0", "right_axis": "0", "right_add": "0",
            "left_sph": "-1.75", "left_cyl": "0", "left_axis": "0", "left_add": "0", "pd": "63"}
    form.update(kw)
    return form


class PrescriptionTest(unittest.TestCase):
    def test_a_plain_prescription_passes(self):
        out, errors = aig.validate_rx(rx(), PRODUCT, SINGLE)
        self.assertEqual(errors, [])
        self.assertEqual(out["right"]["sph"], -2.0)
        self.assertEqual(out["pd"], 63)

    def test_total_power_is_the_manufacturer_range_per_model(self):
        _out, errors = aig.validate_rx(rx(right_sph="-11.00", right_cyl="-1.25", right_axis="90"), PRODUCT, SINGLE)
        self.assertTrue(any("total power" in e for e in errors), errors)
        narrow = dict(PRODUCT, rx_total_min=-6.0, rx_total_max=4.0)
        _out, errors = aig.validate_rx(rx(right_sph="-6.00", right_cyl="-0.50", right_axis="90"), narrow, SINGLE)
        self.assertTrue(any("total power" in e for e in errors), errors)

    def test_axis_is_required_with_cylinder(self):
        _out, errors = aig.validate_rx(rx(right_cyl="-1.00", right_axis="0"), PRODUCT, SINGLE)
        self.assertTrue(any("AXIS" in e for e in errors), errors)

    def test_axis_in_steps_of_five(self):
        _out, errors = aig.validate_rx(rx(right_cyl="-1.00", right_axis="92"), PRODUCT, SINGLE)
        self.assertTrue(any("AXIS" in e for e in errors), errors)
        _out, errors = aig.validate_rx(rx(right_cyl="-1.00", right_axis="95"), PRODUCT, SINGLE)
        self.assertEqual(errors, [])

    def test_quarter_steps_only(self):
        _out, errors = aig.validate_rx(rx(right_sph="-2.10"), PRODUCT, SINGLE)
        self.assertTrue(any("SPH" in e for e in errors), errors)

    def test_add_is_needed_for_progressive_and_ignored_otherwise(self):
        _out, errors = aig.validate_rx(rx(), PRODUCT, PROG)
        self.assertTrue(any("ADD" in e for e in errors), errors)
        out, errors = aig.validate_rx(rx(right_add="1.50", left_add="1.50"), PRODUCT, PROG)
        self.assertEqual(errors, [])
        out, _errors = aig.validate_rx(rx(right_add="1.50"), PRODUCT, SINGLE)
        self.assertEqual(out["right"]["add"], 0.0)

    def test_pd_range(self):
        _out, errors = aig.validate_rx(rx(pd="45"), PRODUCT, SINGLE)
        self.assertTrue(any("PD" in e for e in errors), errors)


class PriceAndDispatchTest(unittest.TestCase):
    def test_offer_against_mrp(self):
        self.assertEqual(aig._price(PRODUCT), {"mrp": 39900, "price": 29925, "off": 25})
        self.assertEqual(aig._price({"product_price": 45700, "product_special_price": None}),
                         {"mrp": 45700, "price": 45700, "off": 0})

    def test_indian_money_format(self):
        self.assertEqual(aig.money(29925), "₹29,925")
        self.assertEqual(aig.money(123456), "₹1,23,456")
        self.assertEqual(aig.money(0), "₹0")

    def test_sales_status_drives_dispatch_and_buying(self):
        today = datetime.date(2026, 10, 31)
        pre = aig._dispatch({"sales_status": "PREORDER"}, today)
        self.assertEqual(pre["date"], "2026-11-30")
        self.assertTrue(pre["can_buy"])
        self.assertTrue(aig._dispatch({"sales_status": "SELLING_FAST"}, today)["can_buy"])
        self.assertFalse(aig._dispatch({"sales_status": "SOLD_OUT"}, today)["can_buy"])
        self.assertEqual(aig._dispatch({"sales_status": None}, today)["status"], "PREORDER")


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SECRET_KEY"] = "test"
        os.environ.pop(aig.FLAG, None)

    def tearDown(self):
        os.environ.pop(aig.FLAG, None)

    def test_hidden_by_default_and_never_on_com(self):
        with self.app.test_request_context("/", base_url="https://optiwar.in"):
            self.assertFalse(aig.is_visible())
        os.environ[aig.FLAG] = "1"
        with self.app.test_request_context("/", base_url="https://optiwar.in"):
            self.assertTrue(aig.is_visible())
        with self.app.test_request_context("/", base_url="https://optiwar.com"):
            self.assertFalse(aig.is_visible())

    def test_chat_section_is_empty_until_visible(self):
        with self.app.test_request_context("/", base_url="https://optiwar.in"):
            self.assertEqual(aig.prompt_section(None, True), "")


if __name__ == "__main__":
    unittest.main()
