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


MON = datetime.datetime(2026, 11, 9, 10, 0)
SAT_AM = datetime.datetime(2026, 11, 14, 11, 45)
SAT_PM = datetime.datetime(2026, 11, 14, 11, 46)
SUN = datetime.datetime(2026, 11, 15, 10, 0)


class NcrRule(unittest.TestCase):

    def test_a_full_ncr_pin_and_a_state_that_agrees_are_both_needed(self):
        for state, z in (("Delhi", "110001"), ("New Delhi", "110016"), ("Haryana", "122018"), ("Gurugram", "122002"),
                         ("Uttar Pradesh", "201301"), ("UP", "201001"), ("Noida", "201304"), ("uttar pradesh", "201102")):
            self.assertTrue(ship.is_ncr(state, z), (state, z))
        for state, z in (("", ""), ("Delhi", ""), ("Delhi", "11"), ("Delhi", "1100"), ("", "110001"),
                         ("Maharashtra", "110001"), ("Delhi", "400001"), ("Delhi", "122018"), ("Haryana", "110001"),
                         ("Haryana", "121001"), ("Uttar Pradesh", "122018")):
            self.assertFalse(ship.is_ncr(state, z), (state, z))


class Schedule(unittest.TestCase):

    def test_never_sunday_and_saturday_only_to_11_45(self):
        self.assertTrue(ship.ncr24_open(MON))
        self.assertTrue(ship.ncr24_open(datetime.datetime(2026, 11, 13, 23, 59)))
        self.assertTrue(ship.ncr24_open(SAT_AM))
        self.assertFalse(ship.ncr24_open(SAT_PM))
        self.assertFalse(ship.ncr24_open(SUN))

    def test_the_page_is_told_when_the_option_closes(self):
        self.assertEqual(ship.ncr24_seconds_left(datetime.datetime(2026, 11, 14, 11, 0)), 45 * 60)
        self.assertEqual(ship.ncr24_seconds_left(datetime.datetime(2026, 11, 13, 11, 45)), 24 * 3600)
        self.assertEqual(ship.ncr24_seconds_left(SUN), 0)
        self.assertEqual(ship.ncr24_seconds_left(SAT_PM), 0)
        self.assertFalse(ship.js_rules(1000, SUN)["ncr24_open"])

    def test_saturday_wording_never_suggests_sunday(self):
        got = ship.choose("ncr24", "Delhi", "110001", now=SAT_AM, ncr24_fee=1000)
        self.assertEqual(got["label"], "Same-day delivery (Delhi NCR)")
        self.assertIn("for delivery today", got["blurb"])
        self.assertNotIn("24", got["label"])
        self.assertTrue(ship.js_rules(1000, SAT_AM)["ncr24_same_day"])
        self.assertFalse(ship.js_rules(1000, MON)["ncr24_same_day"])
        self.assertEqual(ship.choose("ncr24", "Delhi", "110001", now=MON, ncr24_fee=1000)["label"], "24-hour delivery (Delhi NCR)")

    def test_the_page_shows_the_servers_date_for_each_option(self):
        sat = {o["key"]: o["promised"] for o in ship.js_rules(1000, SAT_AM)["options"]}
        self.assertEqual(sat["ncr24"], "Saturday, 14 November 2026")
        self.assertEqual(sat["expedite"], "Monday, 16 November 2026")       # a Sunday promise moves to Monday
        self.assertEqual(ship.js_rules(1000, MON)["ncr_pins"][1][2], "Haryana")
        page = open(os.path.join(REPO, "templates", "checkout.html"), encoding="utf-8").read()
        self.assertIn("est.textContent = opt.promised", page)
        self.assertNotIn("function promised(", page)
        self.assertEqual(page.count('owNcrHint"'), 2)

    def test_india_time_whatever_the_server_zone(self):
        naive = ship.now_ist()
        utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        self.assertAlmostEqual((naive - utc).total_seconds(), 5.5 * 3600, delta=5)


class Availability(unittest.TestCase):

    def test_ncr_gets_three_options_others_two_global_and_preorders_one(self):
        keys = lambda *a, **k: [o["key"] for o in ship.available(*a, **k)]
        self.assertEqual(keys("Delhi", "110001", now=MON), ["standard", "expedite", "ncr24"])
        self.assertEqual(keys("Delhi", "", now=MON), ["standard", "expedite"])
        self.assertEqual(keys("Maharashtra", "400001", now=MON), ["standard", "expedite"])
        self.assertEqual(keys("Delhi", "110001", is_india=False, now=MON), ["standard"])
        self.assertEqual(keys("Delhi", "110001", preorder=True, now=MON), ["standard"])
        self.assertEqual(keys("Delhi", "110001", now=SUN), ["standard", "expedite"])
        self.assertEqual(keys("Delhi", "110001", now=SAT_PM), ["standard", "expedite"])
        self.assertEqual(keys("Delhi", "110001", now=SAT_AM), ["standard", "expedite", "ncr24"])

    def test_a_posted_choice_is_validated_against_the_address_and_the_clock(self):
        self.assertEqual(ship.choose("ncr24", "Delhi", "110001", now=MON)["fee"], 1000)
        self.assertEqual(ship.choose("ncr24", "Maharashtra", "400001", now=MON)["key"], "standard")
        self.assertEqual(ship.choose("ncr24", "Delhi", "110001", now=SUN)["key"], "standard")
        self.assertEqual(ship.choose("EXPEDITE", "Maharashtra", "400001", now=MON)["fee"], 300)
        self.assertEqual(ship.choose("expedite", "Delhi", "110001", preorder=True, now=MON)["key"], "standard")
        self.assertEqual(ship.choose(None, "", "", now=MON)["key"], "standard")

    def test_ops_can_switch_the_service_off(self):
        keys = [o["key"] for o in ship.available("Delhi", "110001", now=MON, ncr24_enabled=False)]
        self.assertEqual(keys, ["standard", "expedite"])
        self.assertEqual(ship.choose("ncr24", "Delhi", "110001", now=MON, ncr24_enabled=False)["key"], "standard")
        self.assertFalse(ship.js_rules(1000, MON, False)["ncr24_open"])
        self.assertTrue(ship.js_rules(1000, MON, True)["ncr24_open"])

    def test_ops_price_is_what_is_shown_and_charged(self):
        got = ship.choose("ncr24", "Delhi", "110001", now=MON, ncr24_fee=1250)
        self.assertEqual((got["fee"], got["blurb"]), (1250, "Rs 1250. Delivered within 24 hours of your order."))
        self.assertEqual(ship.BY_KEY["ncr24"]["fee"], 1000)
        rules = {o["key"]: o for o in ship.js_rules(1250, MON)["options"]}
        self.assertEqual(rules["ncr24"]["fee"], 1250)
        self.assertIn("Rs 1250", rules["ncr24"]["blurb"])

    def test_promised_dates(self):
        sat = datetime.datetime(2026, 11, 7, 18, 0)
        self.assertEqual(ship.promised_date(sat, "standard"), datetime.date(2026, 11, 12))
        self.assertEqual(ship.promised_date(sat, "expedite"), datetime.date(2026, 11, 9))   # Sunday pushed to Monday
        self.assertEqual(ship.promised_date(datetime.date(2026, 11, 9), "ncr24"), datetime.date(2026, 11, 10))
        self.assertEqual(ship.promised_date(SAT_AM, "ncr24"), datetime.date(2026, 11, 14))   # Saturday: shipped and promised that day
        self.assertEqual(ship.promised_date(datetime.datetime(2026, 11, 13, 18, 0), "ncr24"), datetime.date(2026, 11, 14))


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
        self.assertEqual([p[0] for p in rules["ncr_pins"]], ["110", "122", "2010", "2011", "2013"])


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
        self.assertIn("_posted_option, customer_state, customer_postcode, _shipping_ready, bool(_aig_dispatch),", models)
        # what the customer agreed to, or they are asked again
        self.assertIn("safe_float(request.form.get('shipping_fee_seen'), -1) != shipping_fee", models)
        self.assertIn("if shipping_choice['key'] != _posted_option:", models)
        self.assertIn("shipping_options.promised_date(shipping_options.now_ist(),", models)
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
