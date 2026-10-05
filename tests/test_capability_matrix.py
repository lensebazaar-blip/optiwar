"""The capability matrix names only read tools the turn trace records, and
LOOKUP_CART reads names and quantities, never a price."""
import inspect
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.test_chat_attachments import _load_gateway  # noqa: E402


def _cg():
    return sys.modules.get("flaskr.chat_gateway") or _load_gateway()


class Matrix(unittest.TestCase):

    def test_every_read_tool_in_the_matrix_is_recorded_by_the_trace(self):
        with open(os.path.join(REPO, "docs", "AI_CAPABILITY_MATRIX.md"), encoding="utf-8") as fh:
            doc = fh.read()
        named = set(re.findall(r"`([A-Za-z_]+)`", doc.split("| Stage |", 1)[1].split("\n\n")[0]))
        src = inspect.getsource(_cg()._turn_tools)
        for tool in named:
            self.assertIn("'%s'" % tool, src, tool)


class CartLookup(unittest.TestCase):

    def test_it_is_read_on_the_cart_pages_or_when_asked(self):
        cl = _cg().cart_lookup
        self.assertTrue(cl.wanted("hi", "https://optiwar.in/cart"))
        self.assertTrue(cl.wanted("hi", "https://optiwar.com/checkout/"))
        self.assertTrue(cl.wanted("what is in my cart?", "https://optiwar.in/"))
        self.assertTrue(cl.wanted("mere कार्ट me kya hai", "https://optiwar.in/"))
        self.assertFalse(cl.wanted("show me round frames", "https://optiwar.in/frames"))
        self.assertFalse(cl.wanted("cartier style frames", "https://optiwar.in/"))

    def test_names_and_quantities_only(self):
        cl = _cg().cart_lookup
        model = cl.read_model([
            {"product_name": "Aviator Gold", "order_quantity": 2, "product_special_price": 1999},
            {"product_name": "Precision1", "right_qty": 3, "left_qty": 3, "product_price": 26.95},
            "junk"])
        self.assertEqual(model, {"items": [{"name": "Aviator Gold", "quantity": 2},
                                           {"name": "Precision1", "quantity": 6}]})
        text = cl.prompt_section(model)
        self.assertIn("Aviator Gold × 2", text)
        self.assertNotIn("1999", text)
        self.assertNotIn("26.95", text)
        self.assertEqual(cl.event_payload(model), {"lines": 2, "units": 8})
        self.assertIn("The cart is empty.", cl.prompt_section(cl.read_model(None)))

    def test_the_trace_records_the_cart_read(self):
        cg = _cg()
        with _ctx():
            tools = cg._turn_tools(None, None, None, None, None, None, None, "OTHER",
                                   {"items": []})
        self.assertIn({"tool": "LOOKUP_CART", "found": False}, tools)


def _ctx():
    from flask import Flask
    return Flask(__name__).app_context()
