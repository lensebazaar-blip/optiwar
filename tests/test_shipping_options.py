"""Delivery options at checkout (shipping_options.py): the Delhi NCR rule, what
each address may choose, the promised dates, the stored row, and the fee that
joins what the gateway charges. No database."""
import datetime
import importlib.util
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ship = _load("shipping_options_under_test", os.path.join(REPO, "shipping_options.py"))


class _Cursor:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.calls, self._i = list(rows or []), fail, [], 0

    def execute(self, sql, args=None):
        if self.fail:
            raise RuntimeError("Table 'optiwar2.order_shipping' doesn't exist")
        self.calls.append((sql, args))

    def fetchone(self):
        if self._i < len(self.rows):
            row = self.rows[self._i]; self._i += 1; return row
        return None


class NcrRule(unittest.TestCase):

    def test_pin_codes_decide_first(self):
        for z in ("110001", "122018", "201301", "201001", "201102"):
            self.assertTrue(ship.is_ncr("", z), z)
        for z in ("400001", "560001", "121001"):
            self.assertFalse(ship.is_ncr("Delhi", z), z)      # a full non-NCR pin wins over the state

    def test_state_and_city_words_when_the_pin_is_not_there(self):
        self.assertTrue(ship.is_ncr("Delhi", ""))
        self.assertTrue(ship.is_ncr("New Delhi", "11"))
        self.assertTrue(ship.is_ncr("Haryana", "", "Sector 14, Gurgaon"))
        self.assertFalse(ship.is_ncr("Haryana", "", "Panipat"))
        self.assertFalse(ship.is_ncr("", ""))


class Availability(unittest.TestCase):

    def test_ncr_gets_three_options_others_two_global_and_preorders_one(self):
        self.assertEqual([o["key"] for o in ship.available("Delhi", "110001")], ["standard", "expedite", "ncr24"])
        self.assertEqual([o["key"] for o in ship.available("Maharashtra", "400001")], ["standard", "expedite"])
        self.assertEqual([o["key"] for o in ship.available("Delhi", "110001", is_india=False)], ["standard"])
        self.assertEqual([o["key"] for o in ship.available("Delhi", "110001", preorder=True)], ["standard"])

    def test_a_posted_choice_is_validated_against_the_address(self):
        self.assertEqual(ship.choose("ncr24", "Delhi", "110001")["fee"], 1000)
        self.assertEqual(ship.choose("ncr24", "Maharashtra", "400001")["key"], "standard")
        self.assertEqual(ship.choose("EXPEDITE", "Maharashtra", "400001")["fee"], 300)
        self.assertEqual(ship.choose("expedite", "Delhi", "110001", preorder=True)["key"], "standard")
        self.assertEqual(ship.choose(None, "", "")["key"], "standard")

    def test_promised_dates(self):
        sat = datetime.datetime(2026, 11, 7, 18, 0)
        self.assertEqual(ship.promised_date(sat, "standard"), datetime.date(2026, 11, 12))
        self.assertEqual(ship.promised_date(sat, "expedite"), datetime.date(2026, 11, 9))   # Sunday pushed to Monday
        self.assertEqual(ship.promised_date(datetime.date(2026, 11, 9), "ncr24"), datetime.date(2026, 11, 10))


class Storage(unittest.TestCase):

    def test_record_read_fee_and_note(self):
        cur = _Cursor()
        self.assertTrue(ship.record(cur, "ABCDEF-123456", ship.BY_KEY["expedite"], datetime.date(2026, 11, 9), "Delhi", "110001", now="now"))
        sql, args = cur.calls[0]
        self.assertIn("INSERT INTO order_shipping", sql)
        self.assertEqual(args[:4], ("ABCDEF-123456", "expedite", 300, datetime.date(2026, 11, 9)))
        row = {"option_key": "expedite", "fee": 300, "promised_date": datetime.date(2026, 11, 9)}
        self.assertEqual(ship.fee_for(_Cursor([row]), "X"), 300.0)
        self.assertEqual(ship.fee_for(_Cursor(fail=True), "X"), 0.0)
        self.assertIn("EXPEDITE (Rs 300 paid): promised by Monday, 09 November 2026", ship.note(row))
        self.assertEqual(ship.note({"option_key": "standard", "fee": 0}), "")
        self.assertEqual(ship.note(None), "")
        self.assertFalse(ship.record(_Cursor(fail=True), "X", ship.BY_KEY["standard"], None))

    def test_the_gateway_amount_includes_the_fee(self):
        paid_orders = _load("paid_orders_for_shipping", os.path.join(REPO, "paid_orders.py"))
        cur = _Cursor([{"total": 599}, {"fee": 300}])
        self.assertEqual(paid_orders.order_amount_minor(cur, "X"), 89900)
        cur = _Cursor([{"total": 599}])            # no shipping row
        self.assertEqual(paid_orders.order_amount_minor(cur, "X"), 59900)

    def test_no_table_no_fee(self):
        class Cur(_Cursor):
            def fetchall(self):
                return []
        self.assertTrue(ship.ensure_schema(Cur()))
        self.assertFalse(ship.ensure_schema(Cur(fail=True)))
        models = open(os.path.join(REPO, "models.py"), encoding="utf-8").read()
        # GET offers and POST charges only when the table is usable; a paid option that cannot be recorded stops the order
        self.assertIn("and shipping_options.ensure_schema(cursor))", models)
        self.assertIn("_shipping_ready = bool(_is_india) and shipping_options.ensure_schema(cursor)", models)
        self.assertIn("shipping_fee = float(shipping_choice['fee']) if _shipping_ready else 0.0", models)
        self.assertIn('raise RuntimeError("delivery option could not be recorded")', models)

    def test_ddl_is_additive_and_the_page_rules_carry_every_option(self):
        ddl = dict(ship.TABLES)["order_shipping"]
        self.assertIn("CREATE TABLE IF NOT EXISTS", ddl)
        rules = ship.js_rules()
        self.assertEqual([o["key"] for o in rules["options"]], ["standard", "expedite", "ncr24"])
        self.assertEqual(rules["zip_prefixes"], ["110", "122", "2010", "2011", "2013"])


class ListsAndMail(unittest.TestCase):

    def test_my_orders_totals_include_the_fee(self):
        class Cur(_Cursor):
            def fetchall(self):
                return self.rows
        rows = [{"order_id": "A1", "option_key": "expedite", "fee": 300, "promised_date": datetime.date(2026, 11, 9)}]
        got = ship.fees_for(Cur(rows), ["A1", "B2"])
        self.assertEqual(list(got), ["A1"])
        self.assertEqual(ship.fees_for(Cur(fail=True), ["A1"]), {})
        self.assertEqual(ship.fees_for(Cur(), []), {})
        orders = [{"order_id": "A1", "grand_total": 599}, {"order_id": "B2", "grand_total": 699}]
        ship.attach_fees(orders, got)
        self.assertEqual((orders[0]["grand_total"], orders[0]["shipping_fee"], orders[0]["shipping_label"]), (899, 300.0, "Expedite"))
        self.assertEqual((orders[1]["grand_total"], orders[1]["shipping_fee"], orders[1]["shipping_label"]), (699, 0.0, ""))

    def test_the_confirmation_mail_cart_carries_the_fee_and_the_note_without_touching_the_cart(self):
        cart = [{"product_name": "CE-I", "order_quantity": 1, "product_special_price": 599, "ATC_total": 599}]
        row = {"option_key": "expedite", "fee": 300, "promised_date": datetime.date(2026, 11, 9)}
        out = ship.mail_cart(cart, row, "ONE DISPATCH: hold everything.")
        self.assertEqual(len(cart), 1)                                   # the stored cart is not changed
        self.assertEqual([l["product_name"] for l in out],
                         ["CE-I", "Delivery: Expedite (18% GST inclusive; promised by 09 Nov 2026)",
                          "Dispatch instruction: ONE DISPATCH: hold everything."])
        self.assertEqual(sum(l.get("ATC_total") or l.get("server_total_price") or 0 for l in out), 899)   # the charged total
        self.assertEqual(out[2]["ATC_total"], 0)
        # the real row builder in mail.py prints the text
        import ast
        source = open(os.path.join(REPO, "mail.py"), encoding="utf-8").read()
        fn = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "format_cart_details")
        scope = {}
        exec(ast.get_source_segment(source, fn), scope)
        html = scope["format_cart_details"](out)
        self.assertIn("18% GST inclusive; promised by 09 Nov 2026", html)
        self.assertIn("ONE DISPATCH: hold everything.", html)
        self.assertIn("Rs 300", html)
        self.assertEqual(ship.mail_cart(cart, None, ""), cart)
        self.assertEqual(len(ship.mail_cart(cart, {"option_key": "standard", "fee": 0}, "")), 1)


class Wiring(unittest.TestCase):

    def test_checkout_models_and_deploy(self):
        ck = open(os.path.join(REPO, "templates", "checkout.html"), encoding="utf-8").read()
        self.assertIn('id="owShip"', ck)
        self.assertIn('name="shipping_option"', ck)
        self.assertIn('id="owGrandTotal"', ck)
        models = open(os.path.join(REPO, "models.py"), encoding="utf-8").read()
        self.assertIn("shipping_options.choose(", models)
        self.assertIn("grand_total = round(grand_total + shipping_fee, 2)", models)
        self.assertIn("_ship_recorded = shipping_options.record(", models)
        # India only: optiwar.com never gets a row (its success page keeps its own estimate)
        self.assertIn("if _is_india:\n            # optiwar.com keeps its own shipping terms", models)
        # any AI Glasses line with its own dispatch date (pre-order or selling fast) blocks the paid options, GET and POST
        self.assertIn("shipping_allowed = (bool(_req_is_india()) and not _aig_dispatch", models)
        self.assertIn("_shipping_ready, bool(_aig_dispatch), customer_address)", models)
        profile = open(os.path.join(REPO, "profile.py"), encoding="utf-8").read()
        self.assertIn("shipping_options.attach_fees(grouped_orders, shipping_options.fees_for(", profile)
        self.assertIn("shipping_options.fee_for(cursor, order_id)", models)
        self.assertIn("shipping_rules=shipping_rules", models)
        deploy = open(os.path.join(REPO, "deploy", "deploy.py"), encoding="utf-8").read()
        dep_block = deploy[deploy.index("DEPLOY_SET = ("):deploy.index("NEW_IN_RELEASE = (")]
        new_block = deploy[deploy.index("NEW_IN_RELEASE = ("):]
        new_block = new_block[:new_block.index('")\n') + 2]
        self.assertIn('"shipping_options.py"', dep_block)
        self.assertIn('"shipping_options.py"', new_block)
        self.assertIn("shipping_options_module().TABLES", deploy)


if __name__ == "__main__":
    unittest.main()
