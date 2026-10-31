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



class _Cursor:
    """Answers the two queries revalidate makes: which ids are AI Glasses, then their rows."""
    def __init__(self, rows):
        self.rows, self._out = rows, []

    def execute(self, sql, args):
        ids = {str(a) for a in args[1:]}
        hits = [r for r in self.rows if str(r["product_id"]) in ids]
        self._out = [{"product_id": r["product_id"]} for r in hits] if "SELECT product_id FROM" in sql else hits

    def fetchall(self):
        return self._out


ROW = dict(PRODUCT, product_id=7, product_name="Wayfarer Gen 2")


def aig_line(**kw):
    line = {"product_id": "7", "product_name": "Wayfarer Gen 2", "ai_glasses": 1, "aig_lens": "single",
            "order_quantity": 1, "product_special_price": 1, "lens_price": 0, "server_total_price": 1}
    line.update(kw)
    return line


class RevalidateTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SECRET_KEY"] = "test"
        os.environ[aig.FLAG] = "1"

    def tearDown(self):
        os.environ.pop(aig.FLAG, None)

    def run_on(self, host, cart, rows=(ROW,)):
        with self.app.test_request_context("/", base_url="https://" + host):
            return aig.revalidate(_Cursor(list(rows)), cart)

    def test_repriced_from_the_database_for_every_pair(self):
        cart, removed = self.run_on("optiwar.in", [aig_line(order_quantity=3), {"product_id": "99"}])
        self.assertEqual(removed, [])
        self.assertEqual(cart[0]["server_total_price"], (29925 + 4900) * 3)
        self.assertEqual(cart[0]["product_special_price"], 29925)
        self.assertEqual(cart[1], {"product_id": "99"})

    def test_removed_on_com_and_with_the_flag_off(self):
        self.assertEqual(self.run_on("optiwar.com", [aig_line()])[0], [])
        os.environ.pop(aig.FLAG)
        cart, removed = self.run_on("optiwar.in", [aig_line()])
        self.assertEqual((cart, removed), ([], ["Wayfarer Gen 2"]))

    def test_a_line_from_the_older_cart_routes_is_removed(self):
        legacy = {"product_id": "7", "product_name": "Wayfarer Gen 2", "server_total_price": 100}
        self.assertEqual(self.run_on("optiwar.in", [legacy])[0], [])

    def test_sold_out_or_unknown_lens_is_removed(self):
        self.assertEqual(self.run_on("optiwar.in", [aig_line()], [dict(ROW, sales_status="SOLD_OUT")])[0], [])
        self.assertEqual(self.run_on("optiwar.in", [aig_line(aig_lens="free")])[0], [])
        self.assertEqual(self.run_on("optiwar.in", [aig_line()], [dict(ROW, rx_allowed=0)])[0], [])



class FrameListingTest(unittest.TestCase):
    def test_the_all_frames_page_filters_both_its_queries_by_site(self):
        import ast
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models.py")
        with open(path) as fh:
            source = fh.read()
        fn = next(n for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.FunctionDef) and n.name == "all_spectacle_frames")
        self.assertEqual(ast.get_source_segment(source, fn).count("catalogue_site_filter()"), 2)



class DispatchDateTest(unittest.TestCase):
    START = datetime.date(2026, 10, 31)

    def test_a_preorder_ships_thirty_days_out(self):
        self.assertEqual(aig.dispatch_date(_Cursor([ROW]), ["7", "99"], self.START),
                         (datetime.date(2026, 11, 30), True))

    def test_selling_fast_uses_the_longest_window(self):
        row = dict(ROW, sales_status="SELLING_FAST")
        self.assertEqual(aig.dispatch_date(_Cursor([row]), ["7"], self.START), (datetime.date(2026, 11, 7), False))

    def test_no_ai_glasses_or_a_failing_query_keeps_the_usual_date(self):
        self.assertEqual(aig.dispatch_date(_Cursor([ROW]), ["99"], self.START), (None, False))
        self.assertEqual(aig.dispatch_date(None, ["7"], self.START), (None, False))


_po_spec = importlib.util.spec_from_file_location(
    "paid_orders_under_test",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "paid_orders.py"))
paid_orders = importlib.util.module_from_spec(_po_spec)
_po_spec.loader.exec_module(paid_orders)


class _Recorder:
    def __init__(self, fail=False, row=None):
        self.calls, self.fail, self.row = [], fail, row

    def execute(self, sql, args=None):
        if self.fail:
            raise RuntimeError("Table 'optiwar2.ai_glasses_order_dispatch' doesn't exist")
        self.calls.append((sql, args))

    def fetchone(self):
        return self.row


class SealedDispatchTest(unittest.TestCase):
    """A paid AI Glasses line keeps the dispatch date it was promised (Devin Review on #235)."""

    def test_paid_orders_counts_the_same_days_as_ai_glasses(self):
        self.assertEqual(paid_orders.AIG_PREORDER_DAYS, aig.PREORDER_DAYS)
        self.assertEqual(paid_orders.AIG_SELLING_FAST_DAYS, aig.SHIP_DAYS[1])

    def test_sealed_from_the_order_date_once_and_never_for_sold_out(self):
        cur = _Recorder()
        paid_orders.seal_aig_dispatch(cur, "ABCDEF-123456")
        sql, args = cur.calls[0]
        self.assertIn("INSERT IGNORE INTO ai_glasses_order_dispatch", sql)
        self.assertIn("DATE(o.date_created)", sql)
        self.assertIn("IN ('PREORDER', 'SELLING_FAST')", sql)
        self.assertEqual(args, (7, 30, "ABCDEF-123456"))

    def test_a_missing_table_never_breaks_the_payment(self):
        paid_orders.seal_aig_dispatch(_Recorder(fail=True), "ABCDEF-123456")
        self.assertIsNone(aig.promised_dispatch(_Recorder(fail=True), "ABCDEF-123456"))

    def test_the_order_page_reads_the_sealed_date(self):
        day = datetime.date(2026, 11, 30)
        self.assertEqual(aig.promised_dispatch(_Recorder(row={"d": day}), "ABCDEF-123456"), day)
        self.assertIsNone(aig.promised_dispatch(_Recorder(row={"d": None}), "ABCDEF-123456"))
        import ast
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models.py")
        with open(path) as fh:
            source = fh.read()
        fn = next(n for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.FunctionDef) and n.name == "success")
        body = ast.get_source_segment(source, fn)
        self.assertLess(body.index("promised_dispatch"), body.index("ai_glasses.dispatch_date"))
        self.assertIn("_ship = _aig or dispatch_date_obj().date()", body)

    def test_ddl_is_additive(self):
        ddl = dict(aig.TABLES)["ai_glasses_order_dispatch"]
        self.assertIn("CREATE TABLE IF NOT EXISTS", ddl)
        self.assertNotIn("ALTER", ddl.upper())


class FaceMatchStripTest(unittest.TestCase):
    """AI Glasses on the Face Match frames page: only sizes that fit, one card per style."""

    def _run(self, visible, fit, products):
        saved = (aig.is_visible, aig._db, aig.load_products, aig.face_fit_for)

        class _Db:
            def cursor(self):
                return type("C", (), {"close": lambda self: None})()
        aig.is_visible = lambda: visible
        aig._db = lambda: _Db()
        aig.load_products = lambda cur: products
        aig.face_fit_for = lambda sizes: fit
        try:
            return aig.face_matches()
        finally:
            aig.is_visible, aig._db, aig.load_products, aig.face_fit_for = saved

    def _p(self, pid, style, can_buy=True):
        return {"product_id": pid, "family": "WAYFARER_G2", "style_code": style,
                "dispatch": {"can_buy": can_buy}}

    def test_nothing_unless_visible_or_scanned(self):
        prods = [self._p(1, "A")]
        fit = {"state": "checked", "sizes": {1: {"matched": True, "label": "GOOD"}}}
        self.assertEqual(self._run(False, fit, prods)["items"], [])
        self.assertEqual(self._run(True, {"state": "no_scan"}, prods)["items"], [])

    def test_first_fitting_size_per_style_and_never_sold_out(self):
        prods = [self._p(1, "A"), self._p(2, "A"), self._p(3, "B"), self._p(4, "C", can_buy=False)]
        fit = {"state": "checked", "person": "Raj", "sizes": {
            1: {"matched": False, "label": "Not a match"}, 2: {"matched": True, "label": "GOOD"},
            3: {"matched": True, "label": "EXCELLENT"}, 4: {"matched": True, "label": "GOOD"}}}
        out = self._run(True, fit, prods)
        self.assertEqual(out["person"], "Raj")
        self.assertEqual([(i["product_id"], i["fit_label"]) for i in out["items"]], [(2, "GOOD"), (3, "EXCELLENT")])


if __name__ == "__main__":
    unittest.main()
