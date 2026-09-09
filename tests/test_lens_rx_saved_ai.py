"""PR-B: a saved prescription is the customer's own, and an AI proposal is a
suggestion the page checks — neither places anything in the cart by itself.

Unit tests need no database. ``OnMariaDB`` cases prove ownership against the
CI MariaDB and are skipped without one.
"""
import datetime
import importlib.util
import os
import sys
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


lens_order = _load("lens_order")
lens_rx = _load("lens_rx")
lens_prompt = _load("lens_prompt")

LENS = {"product_id": 1015, "product_name": "Precision1 (30 pack)",
        "product_special_price_eur": 15.11, "lens_type": "SPHERICAL",
        "min_boxes_single_eye": 12, "min_boxes_both_per_eye": 6}

VARIANTS = [
    {"variant_id": 41, "sph": "-3.75", "cyl": None, "axis": None,
     "add_power": None, "base_curve": "8.30", "diameter": "14.20",
     "color_code": "", "color_name": None},
    {"variant_id": 42, "sph": "-2.50", "cyl": None, "axis": None,
     "add_power": None, "base_curve": "8.30", "diameter": "14.20",
     "color_code": "", "color_name": None},
]
MINIMUMS = {"single": 12, "both": 6, "waived": False}


def _row(cl_rx_id, customer_id=501, right="-3.75", left="-2.50",
         right_boxes=6, left_boxes=6, created=None, **extra):
    row = {
        "cl_rx_id": cl_rx_id, "rx_id": 8000 + cl_rx_id,
        "customer_id": customer_id, "order_id": "O%d" % cl_rx_id,
        "product_id": 1015, "rx_type": "contact_lens", "source": "MANUAL",
        "document_id": None, "reused_from": None,
        "right_sph": right, "right_cyl": None, "right_axis": None,
        "right_add_power": None, "right_base_curve": "8.30",
        "right_diameter": "14.20", "right_color_code": None,
        "right_variant_id": 41, "right_boxes": right_boxes,
        "left_sph": left, "left_cyl": None, "left_axis": None,
        "left_add_power": None, "left_base_curve": "8.30",
        "left_diameter": "14.20", "left_color_code": None,
        "left_variant_id": 42, "left_boxes": left_boxes,
        "site": "optiwar.com",
        "created_at": created or datetime.datetime(2026, 9, 8, 12, 0),
        "retain_until": datetime.date(2028, 9, 8),
    }
    row.update(extra)
    return row


class _Cursor(object):
    """Answers ``for_customer`` with the rows given, newest first."""

    def __init__(self, rows):
        self.rows, self.statements = rows, []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        self._customer = params[0]

    def fetchall(self):
        return [r for r in self.rows if r["customer_id"] == self._customer]


class SavedRx(unittest.TestCase):
    def test_only_the_customers_own_rows_are_offered(self):
        cur = _Cursor([_row(1, 501), _row(2, 502, right="-6.00")])
        mine = lens_rx.saved_for_customer(cur, 501)
        self.assertEqual([e["cl_rx_id"] for e in mine], [1])
        self.assertIsNone(lens_rx.saved_entry(cur, 501, 2))
        self.assertEqual(lens_rx.saved_entry(cur, 501, 1)["cl_rx_id"], 1)

    def test_a_posted_id_names_nothing_without_a_customer(self):
        cur = _Cursor([_row(1, 501)])
        self.assertEqual(lens_rx.saved_for_customer(cur, None), [])
        self.assertIsNone(lens_rx.saved_entry(cur, None, 1))

    def test_contact_lens_rows_only_are_selected(self):
        cur = _Cursor([_row(1, 501)])
        lens_rx.saved_for_customer(cur, 501)
        sql, params = cur.statements[0]
        self.assertIn("rx_type = %s", sql)
        self.assertIn(lens_rx.RX_TYPE_CONTACT_LENS, params)

    def test_two_orders_with_the_same_values_are_one_saved_prescription(self):
        newer = datetime.datetime(2026, 9, 9)
        cur = _Cursor([_row(3, created=newer), _row(1)])
        mine = lens_rx.saved_for_customer(cur, 501)
        self.assertEqual([e["cl_rx_id"] for e in mine], [3])
        self.assertEqual(mine[0]["created_at"], "2026-09-09")

    def test_the_offer_carries_values_for_review_but_no_boxes_or_money(self):
        cur = _Cursor([_row(1, right_boxes=12, left_boxes=0)])
        (entry,) = lens_rx.saved_for_customer(cur, 501)
        self.assertEqual(entry["eyes"]["right"]["sph"], "-3.75")
        self.assertEqual(entry["eyes"]["right"]["bc"], "8.30")
        self.assertEqual(entry["eyes"]["right"]["dia"], "14.20")
        self.assertEqual(entry["eyes"]["right"]["variant_id"], "41")
        self.assertNotIn("boxes", entry["eyes"]["right"])
        self.assertIsNone(entry["eyes"]["left"])
        self.assertEqual(lens_rx.saved_summary(entry), "R -3.75")
        form = lens_rx.saved_form(entry)
        self.assertEqual(form["right_sph"], "-3.75")
        self.assertNotIn("right_boxes", form)   # the customer chooses boxes
        self.assertEqual(form["left_boxes"], "0")

    def test_saved_values_go_through_the_current_matrix_like_typed_ones(self):
        entry = lens_rx.saved_entry(_Cursor([_row(1, right="-9.00")]), 501, 1)
        form = lens_rx.saved_form(entry)
        form["right_boxes"], form["left_boxes"] = "6", "6"
        sels = [lens_order.read_eye(form, e) for e in lens_order.EYES]
        _lines, problems = lens_order.validate_detailed(
            VARIANTS, LENS, sels, site="optiwar.com")
        self.assertTrue(problems)   # -9.00 is not made; the reuse is refused

    def test_a_reuse_is_a_reuse_only_while_the_values_are_the_saved_ones(self):
        entry = lens_rx.saved_entry(_Cursor([_row(1)]), 501, 1)
        form = lens_rx.saved_form(entry)
        form["right_boxes"], form["left_boxes"] = "6", "6"
        sels = [lens_order.read_eye(form, e) for e in lens_order.EYES]
        self.assertTrue(lens_rx.selections_match_saved(entry, sels))
        form["left_sph"] = "-3.75"   # edited after loading: typed, not reused
        sels = [lens_order.read_eye(form, e) for e in lens_order.EYES]
        self.assertFalse(lens_rx.selections_match_saved(entry, sels))
        # Dropping an eye the saved prescription covers is also a change.
        form = lens_rx.saved_form(entry)
        form["right_boxes"], form["left_boxes"] = "12", "0"
        sels = [lens_order.read_eye(form, e) for e in lens_order.EYES]
        self.assertFalse(lens_rx.selections_match_saved(entry, sels))


class Provenance(unittest.TestCase):
    def test_the_cart_item_carries_the_source_to_the_order(self):
        sels = [lens_order.read_eye(
            {"right_sph": "-3.75", "right_boxes": "6",
             "left_sph": "-2.50", "left_boxes": "6"}, e)
            for e in lens_order.EYES]
        lines, problems = lens_order.validate_detailed(
            VARIANTS, LENS, sels, site="optiwar.com")
        self.assertEqual(problems, [])
        item = lens_order.cart_item(LENS, lines)
        self.assertIsNone(item["rx_id"])
        self.assertIsNone(item["rx_source"])   # MANUAL until a route vouches
        self.assertIn("reused_from", item)

    def test_record_writes_the_items_source_and_reuse(self):
        class Rec(object):
            def __init__(self):
                self.statements, self.lastrowid = [], 99

            def execute(self, sql, params=None):
                self.statements.append((sql, params))

            def fetchone(self):
                return None
        cur = Rec()
        item = {"product_id": 1015, "product_name": "P1",
                "product_category": "Contact Lenses",
                "vertical": "CONTACT_LENS", "product_special_price": 15.11,
                "rx_id": None, "rx_source": lens_rx.SOURCE_SAVED_REUSED,
                "reused_from": 7, "right_qty": 6, "right_pwr": "-3.75",
                "right_bc": "8.3", "right_dia": "14.2", "left_qty": 6,
                "left_pwr": "-2.50", "left_bc": "8.3", "left_dia": "14.2"}
        lens_rx.record(cur, item, 501, "ORD1", "optiwar.com")
        insert = [p for s, p in cur.statements
                  if "INSERT INTO contact_lens_prescriptions" in s][0]
        self.assertIn("SAVED_REUSED", insert)
        self.assertIn(7, insert)
        # An item saying nothing is what it always was.
        cur = Rec()
        item.pop("rx_source"), item.pop("reused_from")
        lens_rx.record(cur, item, 501, "ORD2", "optiwar.com")
        insert = [p for s, p in cur.statements
                  if "INSERT INTO contact_lens_prescriptions" in s][0]
        self.assertIn("MANUAL", insert)


class AIProposal(unittest.TestCase):
    REPLY = ("So right eye -3.75 and left -2.50; the page will confirm.\n"
             '[LENS_RX:{"right":{"sph":"-3.75"},"left":{"pwr":"-2.5"}}]')

    def test_the_tag_is_stripped_and_read_canonically(self):
        reply, proposal = lens_rx.extract_proposal(self.REPLY)
        self.assertNotIn("LENS_RX", reply)
        self.assertEqual(proposal["right"]["sph"], "-3.75")
        self.assertEqual(proposal["left"]["sph"], "-2.50")

    def test_no_tag_no_proposal_and_a_broken_tag_is_nothing(self):
        self.assertEqual(lens_rx.extract_proposal("hello"), ("hello", None))
        reply, proposal = lens_rx.extract_proposal("x [LENS_RX:{oops}]")
        self.assertIsNone(proposal)
        self.assertNotIn("LENS_RX", reply)
        self.assertIsNone(lens_rx.extract_proposal(
            '[LENS_RX:{"right":{"cyl":"-0.75"}}]')[1])   # no power: nothing

    def test_a_proposal_is_validated_by_the_lens_before_it_is_kept(self):
        _r, proposal = lens_rx.extract_proposal(self.REPLY)
        sels = lens_rx.proposal_selections(proposal, MINIMUMS)
        self.assertEqual([s["boxes"] for s in sels], [6, 6])
        lines, problems = lens_order.validate_detailed(
            VARIANTS, LENS, sels, site="optiwar.com")
        self.assertEqual(problems, [])
        form = lens_rx.proposal_form(proposal, sels)
        self.assertEqual(form["right_sph"], "-3.75")
        self.assertEqual(form["left_boxes"], "6")
        # The cards re-read that form exactly as if typed: same validator.
        again = [lens_order.read_eye(form, e) for e in lens_order.EYES]
        self.assertEqual(lens_order.validate_detailed(
            VARIANTS, LENS, again, site="optiwar.com")[1], [])

    def test_a_value_the_lens_is_not_made_in_is_refused(self):
        _r, proposal = lens_rx.extract_proposal(
            '[LENS_RX:{"right":{"sph":"-9.00"}}]')
        sels = lens_rx.proposal_selections(proposal, MINIMUMS)
        self.assertEqual(sels[0]["boxes"], 12)   # single eye minimum
        _lines, problems = lens_order.validate_detailed(
            VARIANTS, LENS, sels, site="optiwar.com")
        self.assertTrue(problems)

    def test_confirmed_values_must_be_the_proposed_ones(self):
        _r, proposal = lens_rx.extract_proposal(self.REPLY)
        sels = lens_rx.proposal_selections(proposal, MINIMUMS)
        self.assertTrue(lens_rx.selections_match_saved(
            {"eyes": proposal}, sels))
        sels[0]["sph"] = "-2.50"
        self.assertFalse(lens_rx.selections_match_saved(
            {"eyes": proposal}, sels))

    def test_the_prompt_states_the_page_and_the_tag_and_forbids_inventing(self):
        text = lens_prompt.pdp_context_section(
            {"product_id": 1015, "product_name": "Precision1",
             "brand": "Alcon", "lens_type": "SPHERICAL", "modality": "DAILY"},
            minimums=MINIMUMS, eyes_state={"right": True, "left": False},
            saved_count=1)
        self.assertIn("[LENS_RX:", text)
        self.assertIn("invent", text.lower())
        self.assertIn("12", text)


class Wiring(unittest.TestCase):
    def _read(self, name):
        with open(os.path.join(REPO, name), encoding="utf-8") as fh:
            return fh.read()

    def test_the_widget_sends_only_booleans_as_page_state(self):
        widget = self._read("static/js/chat-widget.js")
        self.assertIn("page_state", widget)
        self.assertIn("owLensPageState", widget)
        cards = self._read("templates/_lens_eye_cards.html")
        self.assertIn("window.owLensPageState = function", cards)
        state = cards[cards.index("window.owLensPageState"):][:400]
        self.assertNotIn("sph", state)

    def test_the_eye_cards_confirm_a_saved_or_proposed_prescription(self):
        cards = self._read("templates/_lens_eye_cards.html")
        self.assertIn('name="reused_from"', cards)
        self.assertIn('name="rx_source" data-role="rx-source"', cards)
        self.assertIn("'AI_ASSISTED_CONFIRMED'", cards)
        self.assertIn("'UPLOADED_CONFIRMED' if proposal_source == 'upload'", cards)
        # "Use" is a button that applies the entry on this page; a link to
        # ?saved= was a fragment-only navigation the second time round.
        self.assertIn('data-role="use-saved"', cards)
        self.assertIn('data-cl-rx-id="{{ entry.cl_rx_id }}"', cards)
        self.assertNotIn("saved={{ entry.cl_rx_id }}", cards)
        # Both pre-fills are inside the form the customer submits: nothing is
        # added to the cart until they press the button.
        self.assertLess(cards.index("<form action"), cards.index("reused_from"))

    def test_the_route_resolves_provenance_from_the_server_not_the_form(self):
        models = self._read("models.py")
        self.assertIn("def _lens_provenance(cursor, lens, form, selections)",
                      models)
        self.assertIn("lens_rx.saved_entry(cursor, session.get('user_id')",
                      models)
        self.assertIn("selections_match_saved", models)
        # The widget's page_state is context for the prompt only.
        gateway = self._read("chat_gateway.py")
        self.assertIn("lens_rx.PROPOSAL_SESSION_KEY", gateway)
        self.assertIn("validate_detailed", gateway)

    def test_the_deploy_set_carries_every_file_this_change_touches(self):
        deploy = self._read("deploy/deploy.py")
        for name in ("static/js/chat-widget.js", "lens_prompt.py",
                     "lens_rx.py", "templates/_lens_eye_cards.html",
                     "chat_gateway.py", "templates/base.html"):
            self.assertIn('"%s"' % name, deploy)


def _connect():
    host = os.environ.get("OPTIWAR_TEST_MYSQL_HOST")
    if not host:
        return None
    import pymysql
    return pymysql.connect(
        host=host, port=int(os.environ.get("OPTIWAR_TEST_MYSQL_PORT", 3306)),
        user=os.environ.get("OPTIWAR_TEST_MYSQL_USER", "root"),
        password=os.environ.get("OPTIWAR_TEST_MYSQL_PASSWORD", ""),
        database=os.environ.get("OPTIWAR_TEST_MYSQL_DB", "optiwar_test"),
        autocommit=False, cursorclass=pymysql.cursors.DictCursor)


class OnMariaDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = _connect()
        if cls.db is None:
            raise unittest.SkipTest("no MariaDB (OPTIWAR_TEST_MYSQL_*)")
        with cls.db.cursor() as cur:
            cur.execute(lens_rx.SCHEMA)
        cls.db.commit()

    @classmethod
    def tearDownClass(cls):
        if cls.db is not None:
            cls.db.close()

    def test_ownership_holds_in_the_database(self):
        mine, theirs = 90001 + os.getpid() % 1000, 91001 + os.getpid() % 1000
        with self.db.cursor() as cur:
            for customer in (mine, theirs):
                cur.execute(
                    "INSERT INTO contact_lens_prescriptions (customer_id, "
                    "order_id, product_id, source, right_sph, right_boxes, "
                    "right_base_curve, right_diameter, created_at, "
                    "retain_until) VALUES (%s,%s,1015,'MANUAL',-3.75,6,8.3,"
                    "14.2,NOW(),'2028-01-01')",
                    (customer, "T" + uuid.uuid4().hex[:12]))
            self.db.commit()
            cur.execute("SELECT cl_rx_id FROM contact_lens_prescriptions "
                        "WHERE customer_id=%s ORDER BY cl_rx_id DESC LIMIT 1",
                        (theirs,))
            other_id = cur.fetchone()["cl_rx_id"]
            self.assertIsNone(lens_rx.saved_entry(cur, mine, other_id))
            self.assertIsNotNone(lens_rx.saved_entry(cur, theirs, other_id))
            self.assertTrue(all(e["cl_rx_id"] != other_id
                                for e in lens_rx.saved_for_customer(cur, mine)))
        self.db.commit()


if __name__ == "__main__":
    unittest.main()
