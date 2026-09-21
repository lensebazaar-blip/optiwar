"""A frame in the cart is for one person, and an order remembers who.

The choice sits on the cart line: changing it changes the fit shown against
that line and nothing else. A placed order seals the person and the fit as
they were; the profile may then be renamed, rescanned or deleted without the
order changing, while a live cart line whose person is deleted falls back to
"No person / Gift" with its product untouched.
"""
import copy
import importlib.util
import json
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_face_profiles import (  # noqa: E402
    AVAILABLE, C1, C2, LEGACY_DDL, _connect, _load_api, _load_service, _wipe,
)
from test_face_fit import MEAS, PRODUCTS_DDL  # noqa: E402

PERSISTENT_CART_DDL = """
CREATE TABLE IF NOT EXISTS persistent_cart (
    customer_id INT NOT NULL PRIMARY KEY,
    cart_json   LONGTEXT NOT NULL
) ENGINE=InnoDB
"""


def _frame_line(product_id, code, price=1999):
    return {"product_id": product_id, "product_name": "Frame " + code,
            "product_special_price": price, "product_price": price + 500,
            "order_quantity": 2, "ATC_total": price * 2,
            "product_code": code, "rx_id": 17, "right_eye": "-1.00",
            "left_eye": "-1.25", "lens_price": 800, "cyl_price_increase": 0,
            "add_price_increase": 0, "recommendations": [],
            "product_category": "Spectacles Frame"}


def _lens_line():
    return {"product_id": 9800103, "product_name": "A lens",
            "product_category": "Contact Lenses", "vertical": "CONTACT_LENS",
            "right_qty": 1, "left_qty": 1, "ATC_total": 30,
            "order_quantity": 2, "product_code": "LENS1"}


class LineRuleTests(unittest.TestCase):
    """The pure rules: which lines carry a person, and the nobody value."""

    @classmethod
    def setUpClass(cls):
        fp = _load_service()
        pkg_name = "fc_pkg"
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [REPO]
        sys.modules[pkg_name] = pkg
        sys.modules[pkg_name + ".face_profiles"] = fp
        for name in ("lens_cart", "face_fit", "face_cart"):
            spec = importlib.util.spec_from_file_location(
                pkg_name + "." + name, os.path.join(REPO, name + ".py"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
        cls.fc = mod

    def test_a_lens_line_is_not_a_frame_line(self):
        self.assertTrue(self.fc.is_frame_line(_frame_line(1, "A")))
        self.assertFalse(self.fc.is_frame_line(_lens_line()))
        legacy = {"product_id": 5, "product_category": "Contact Lenses", "right_qty": 2}
        self.assertFalse(self.fc.is_frame_line(legacy))

    def test_only_a_spectacle_frame_is_a_frame_line(self):
        for cat in ("Hearing Aids", "category_not_defined", None, ""):
            item = _frame_line(2, "B")
            item["product_category"] = cat
            self.assertFalse(self.fc.is_frame_line(item), cat)
        item = _frame_line(3, "C")
        item["product_category"] = " Spectacles Frame "
        self.assertTrue(self.fc.is_frame_line(item))

    def test_a_garbage_person_is_refused_and_a_stored_one_reads_as_nobody(self):
        for bad in ("abc", -1, "-4", 1.5, [1]):
            with self.assertRaises(self.fc.LineError):
                self.fc._normalise(bad)
        self.assertEqual(self.fc._stored({self.fc.LINE_KEY: "abc"}), 0)
        self.assertEqual(self.fc._stored({self.fc.LINE_KEY: "12"}), 12)
        self.assertEqual(self.fc._stored({}), 0)

    def test_nobody_is_zero_and_is_labelled(self):
        self.assertEqual(self.fc.NOBODY, 0)
        self.assertEqual(self.fc.NO_PERSON_LABEL, "No person / Gift")
        self.assertEqual(self.fc.LINE_KEY, "face_profile_id")

    def test_schema_is_write_once_per_order_line(self):
        ddl = self.fc.SNAPSHOTS_SCHEMA
        self.assertIn("UNIQUE KEY uq_order_face_line (order_id, line_no)", ddl)
        self.assertEqual([n for n, _ in self.fc.TABLES], ["order_face_snapshots"])


@unittest.skipUnless(AVAILABLE, "MariaDB test database not reachable")
class CartAssignmentTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Blueprint, Flask
        cls.fp = _load_service()
        cls.db = _connect()
        cur = cls.db.cursor()
        cur.execute(LEGACY_DDL)
        cur.execute("DROP TABLE IF EXISTS contact_lens_variants")
        cur.execute("DROP TABLE IF EXISTS contact_lens_param_rules")
        cur.execute("DROP TABLE IF EXISTS contact_lens_images")
        cur.execute("DROP TABLE IF EXISTS contact_lens_products")
        cur.execute("DROP TABLE IF EXISTS products")
        cur.execute(PRODUCTS_DDL)
        cur.execute("INSERT INTO products (product_id, product_code, product_name, "
                    "product_size, product_category) VALUES "
                    "(9800101, 'FIT1', 'Fits', '52-18-140', 'Spectacles Frame'),"
                    "(9800102, 'FIT2', 'Wide', '58-22-160', 'Spectacles Frame'),"
                    "(9800103, 'LENS1', 'A lens', '', 'Contact Lenses')")
        cur.execute(PERSISTENT_CART_DDL)
        cur.execute("DROP TABLE IF EXISTS order_face_snapshots")
        cls.db.commit()
        cls.fp.ensure_schema(cls.db)
        cls.api = _load_api(cls.fp, lambda: cls.db)
        cls.fc = sys.modules["fp_pkg.face_cart"]
        cls.fc.ensure_schema(cls.db)
        cls.app = Flask(__name__)
        cls.app.config.update(TESTING=True, SECRET_KEY="test",
                              FACE_PROFILES_ENABLED="1",
                              FACE_PROFILES_ALLOW_EMAILS="lensebazaar@gmail.com")
        bp = Blueprint("main", __name__)
        cls.api.register(bp)
        cls.app.register_blueprint(bp)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        _wipe(cls.db, C1, C2)
        cur = cls.db.cursor()
        cur.execute("DELETE FROM persistent_cart WHERE customer_id IN (%s, %s)", (C1, C2))
        cur.execute("DELETE FROM order_face_snapshots WHERE customer_id IN (%s, %s)", (C1, C2))
        cur.execute("DROP TABLE IF EXISTS products")
        cls.db.commit()
        cls.db.close()

    def setUp(self):
        _wipe(self.db, C1, C2)
        cur = self.db.cursor()
        cur.execute("DELETE FROM persistent_cart WHERE customer_id IN (%s, %s)", (C1, C2))
        cur.execute("DELETE FROM order_face_snapshots WHERE customer_id IN (%s, %s)", (C1, C2))
        self.db.commit()
        self.me = self.fp.ensure_self(self.db, C1, "Sudhanshu")
        self.mother = self.fp.create_profile(self.db, C1, "Mother", "parent", consent=True)
        self.fp.record_scan(self.db, C1, self.mother["id"], MEAS)
        self.stranger = self.fp.ensure_self(self.db, C2, "Other")
        self.cart = [_frame_line(9800101, "FIT1"), _lens_line(),
                     _frame_line(9800102, "FIT2", price=2999)]
        self._login(C1, "lensebazaar@gmail.com", "Sudhanshu", self.cart)

    def _login(self, cid, email, name, cart=None):
        with self.client.session_transaction() as s:
            s.clear()
            s["user_id"] = cid
            s["user_email"] = email
            s["user_name"] = name
            if cart is not None:
                s["cart"] = cart

    def _session_cart(self):
        with self.client.session_transaction() as s:
            return s.get("cart")

    def _persisted_cart(self, cid=C1):
        cur = self.db.cursor()
        cur.execute("SELECT cart_json FROM persistent_cart WHERE customer_id=%s", (cid,))
        row = cur.fetchone()
        return json.loads(row["cart_json"]) if row else None

    def _assign(self, index, product_id, pid):
        return self.client.post("/api/cart/face-assign",
                                json={"index": index, "product_id": product_id,
                                      "face_profile_id": pid})

    def _commercial(self, item):
        return {k: v for k, v in item.items() if k != self.fc.LINE_KEY}

    # -- assigning --------------------------------------------------------

    def test_assigning_a_person_changes_only_that_lines_fit(self):
        before = copy.deepcopy(self.cart)
        r = self._assign(0, 9800101, self.mother["id"])
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertEqual(j["face_profile_id"], self.mother["id"])
        self.assertEqual(j["profile"]["display_name"], "Mother")
        self.assertEqual(j["fit"]["classification"], "excellent")
        cart = self._session_cart()
        self.assertEqual(cart[0][self.fc.LINE_KEY], self.mother["id"])
        self.assertEqual(self._commercial(cart[0]), before[0])
        self.assertEqual(cart[1], before[1])
        self.assertEqual(cart[2], before[2])
        # and the persistent cart carries the same choice
        self.assertEqual(self._persisted_cart()[0][self.fc.LINE_KEY], self.mother["id"])

    def test_two_frame_lines_can_be_for_two_people(self):
        self._assign(0, 9800101, self.mother["id"])
        r = self._assign(2, 9800102, self.me["id"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["fit"]["classification"], "no_measurement")
        cart = self._session_cart()
        self.assertEqual(cart[0][self.fc.LINE_KEY], self.mother["id"])
        self.assertEqual(cart[2][self.fc.LINE_KEY], self.me["id"])

    def test_no_person_is_explicit_zero_and_has_no_fit(self):
        self._assign(0, 9800101, self.mother["id"])
        for nobody in (0, None, "0"):
            r = self._assign(0, 9800101, nobody)
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertEqual(j["face_profile_id"], 0)
            self.assertIsNone(j["profile"])
            self.assertEqual(j["fit"]["classification"], "no_person")
        self.assertEqual(self._session_cart()[0][self.fc.LINE_KEY], 0)

    def test_a_strangers_profile_is_404_and_nothing_changes(self):
        r = self._assign(0, 9800101, self.stranger["id"])
        self.assertEqual(r.status_code, 404)
        self.assertNotIn(self.fc.LINE_KEY, self._session_cart()[0])
        self.assertIsNone(self._persisted_cart())

    def test_a_contact_lens_line_takes_no_person(self):
        r = self._assign(1, 9800103, self.mother["id"])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "not_a_frame")
        self.assertNotIn(self.fc.LINE_KEY, self._session_cart()[1])

    def test_a_stale_page_cannot_change_the_wrong_line(self):
        r = self._assign(0, 9800102, self.mother["id"])   # product of line 2
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"], "line_mismatch")
        r = self._assign(7, 9800101, self.mother["id"])   # no such line
        self.assertEqual(r.status_code, 404)
        r = self._assign("x", 9800101, self.mother["id"])
        self.assertEqual(r.status_code, 400)
        self.assertNotIn(self.fc.LINE_KEY, self._session_cart()[0])

    def test_a_negative_index_never_reaches_a_line_from_the_end(self):
        r = self._assign(-1, 9800102, self.mother["id"])   # -1 is line 2's product
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "bad_line")
        self.assertTrue(all(self.fc.LINE_KEY not in i for i in self._session_cart()))

    def test_a_nonnumeric_person_is_a_400_not_a_500(self):
        r = self._assign(0, 9800101, "mother")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "bad_profile")
        self.assertNotIn(self.fc.LINE_KEY, self._session_cart()[0])

    def test_anonymous_and_gated_off_accounts_are_refused(self):
        with self.client.session_transaction() as s:
            s.clear()
        self.assertEqual(self._assign(0, 9800101, 0).status_code, 401)
        self._login(C2, "someone@example.com", "Other", [_frame_line(9800101, "FIT1")])
        self.assertEqual(self._assign(0, 9800101, 0).status_code, 404)

    # -- defaults and the checkout view ----------------------------------

    def test_a_new_frame_line_defaults_to_the_person_being_shopped_for(self):
        with self.app.test_request_context():
            from flask import session
            session["user_id"] = C1
            session[self.fc.face_fit.SESSION_KEY] = self.mother["id"]
            cart = copy.deepcopy(self.cart)
            self.assertTrue(self.fc.default_lines(self.db, C1, session, cart))
            self.assertEqual(cart[0][self.fc.LINE_KEY], self.mother["id"])
            self.assertNotIn(self.fc.LINE_KEY, cart[1])
            self.assertEqual(cart[2][self.fc.LINE_KEY], self.mother["id"])
            # a second pass changes nothing
            self.assertFalse(self.fc.default_lines(self.db, C1, session, cart))
            # explicit nobody on the page means nobody on the line
            session[self.fc.face_fit.SESSION_KEY] = self.fc.face_fit.NOBODY
            fresh = [_frame_line(9800101, "FIT1")]
            self.assertTrue(self.fc.default_lines(self.db, C1, session, fresh))
            self.assertEqual(fresh[0][self.fc.LINE_KEY], 0)

    def test_decorate_lists_people_and_a_fit_per_frame_line(self):
        cart = copy.deepcopy(self.cart)
        cart[0][self.fc.LINE_KEY] = self.mother["id"]
        cart[2][self.fc.LINE_KEY] = 0
        view = self.fc.decorate(self.db, C1, cart)
        self.assertEqual([p["display_name"] for p in view["people"]], ["Sudhanshu", "Mother"])
        self.assertEqual(view["nobody_label"], "No person / Gift")
        self.assertEqual(set(view["lines"]), {0, 2})
        self.assertEqual(view["lines"][0]["profile_id"], self.mother["id"])
        self.assertEqual(view["lines"][0]["fit"]["classification"], "excellent")
        self.assertEqual(view["lines"][2]["profile_id"], 0)
        self.assertEqual(view["lines"][2]["fit"]["classification"], "no_person")
        self.assertNotIn("capture_path", str(view))

    def test_checkout_template_shows_the_person_on_frame_lines_only(self):
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(os.path.join(REPO, "templates")))
        src = env.loader.get_source(env, "checkout.html")[0]
        self.assertIn("ow-ck-face-select", src)
        self.assertIn("/api/cart/face-assign", src)
        # the selector sits inside the non-lens branch of the line loop
        lens_branch = src.index("ow-ck-item--lens")
        self.assertGreater(src.index('data-face-line="{{ loop.index0 }}"'), lens_branch)
        self.assertIn("{% if face_lines and (loop.index0 in face_lines.lines) %}", src)
        self.assertIn("face_lines.nobody_label", src)

    # -- orders ----------------------------------------------------------

    def _place(self, order_id, cart):
        cur = self.db.cursor()
        n = self.fc.record(self.db, cur, order_id, C1, cart)
        self.db.commit()
        return n

    def test_an_order_seals_the_person_and_fit_per_frame_line(self):
        cart = copy.deepcopy(self.cart)
        cart[0][self.fc.LINE_KEY] = self.mother["id"]
        cart[2][self.fc.LINE_KEY] = 0
        self.assertEqual(self._place("OW-TEST-1", cart), 2)
        cur = self.db.cursor()
        rows = self.fc.for_order(cur, "OW-TEST-1")
        self.assertEqual([r["line_no"] for r in rows], [1, 3])
        first = rows[0]
        self.assertEqual(first["product_id"], 9800101)
        self.assertEqual(first["face_profile_id"], self.mother["id"])
        self.assertEqual(first["display_name"], "Mother")
        self.assertEqual(first["relationship_type"], "parent")
        self.assertEqual(first["is_self"], 0)
        self.assertEqual(first["product_size"], "52-18-140")
        self.assertEqual(first["fit_classification"], "excellent")
        self.assertEqual(float(first["measurements"]["pd_far"]), 63.0)
        self.assertIsNotNone(first["face_scan_id"])
        self.assertIsNotNone(first["measured_at"])
        self.assertEqual(first["fit"]["classification"], "excellent")
        gift = rows[1]
        self.assertIsNone(gift["face_profile_id"])
        self.assertIsNone(gift["display_name"])
        self.assertEqual(gift["person"], "No person / Gift")
        self.assertEqual(first["person"], "Mother")
        self.assertEqual(gift["fit_classification"], "no_person")
        self.assertEqual(gift["product_id"], 9800102)

    def test_a_cart_with_no_choice_yet_still_seals_nobody_per_frame_line(self):
        cart = copy.deepcopy(self.cart)
        for item in cart:
            item.pop(self.fc.LINE_KEY, None)
        self.assertEqual(self._place("OW-TEST-0", cart), 2)
        cur = self.db.cursor()
        rows = self.fc.for_order(cur, "OW-TEST-0")
        self.assertEqual([r["line_no"] for r in rows], [1, 3])
        self.assertTrue(all(r["person"] == "No person / Gift" for r in rows))
        self.assertTrue(all(r["fit_classification"] == "no_person" for r in rows))

    def test_a_hearing_aid_line_gets_no_selector_and_no_snapshot(self):
        cart = copy.deepcopy(self.cart)
        cart[0]["product_category"] = "Hearing Aids"
        self.assertNotIn(0, self.fc.decorate(self.db, C1, cart)["lines"])
        self.assertEqual(self._place("OW-TEST-HA", cart), 1)
        cur = self.db.cursor()
        self.assertEqual([r["line_no"] for r in self.fc.for_order(cur, "OW-TEST-HA")], [3])

    def test_a_snapshot_is_written_once(self):
        cart = copy.deepcopy(self.cart)
        cart[0][self.fc.LINE_KEY] = self.mother["id"]
        self._place("OW-TEST-2", cart)
        cart[0][self.fc.LINE_KEY] = 0
        self.assertEqual(self._place("OW-TEST-2", cart), 0)
        cur = self.db.cursor()
        rows = self.fc.for_order(cur, "OW-TEST-2")
        self.assertEqual(rows[0]["display_name"], "Mother")

    def test_rename_rescan_and_delete_leave_the_order_untouched(self):
        cart = copy.deepcopy(self.cart)
        cart[0][self.fc.LINE_KEY] = self.mother["id"]
        self._place("OW-TEST-3", cart)
        cur = self.db.cursor()
        sealed = self.fc.for_order(cur, "OW-TEST-3")
        self.fp.rename_profile(self.db, C1, self.mother["id"], display_name="Mum")
        self.fp.record_scan(self.db, C1, self.mother["id"], dict(MEAS, pd_far=70.0))
        self.fp.delete_profile(self.db, C1, self.mother["id"])
        self.assertEqual(self.fc.for_order(cur, "OW-TEST-3"), sealed)
        self.assertEqual(sealed[0]["display_name"], "Mother")
        self.assertEqual(float(sealed[0]["measurements"]["pd_far"]), 63.0)

    # -- deletion fallback -----------------------------------------------

    def test_deleting_a_person_resets_live_cart_lines_to_nobody(self):
        self._assign(0, 9800101, self.mother["id"])
        self._assign(2, 9800102, self.mother["id"])
        before = copy.deepcopy(self._session_cart())
        r = self.client.get("/api/face-profiles/%d/references" % self.mother["id"])
        self.assertEqual(r.get_json()["references"]["cart_items"], 2)
        r = self.client.delete("/api/face-profiles/%d" % self.mother["id"])
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["cart_lines_reset"], 2)
        cart = self._session_cart()
        for i in (0, 2):
            self.assertEqual(cart[i][self.fc.LINE_KEY], 0)
            self.assertEqual(self._commercial(cart[i]), self._commercial(before[i]))
        self.assertEqual(cart[1], before[1])
        persisted = self._persisted_cart()
        self.assertEqual(persisted[0][self.fc.LINE_KEY], 0)
        self.assertEqual(persisted[2][self.fc.LINE_KEY], 0)

    def test_a_persisted_cart_on_another_device_is_reset_too(self):
        cart = copy.deepcopy(self.cart)
        cart[0][self.fc.LINE_KEY] = self.mother["id"]
        cur = self.db.cursor()
        cur.execute("INSERT INTO persistent_cart (customer_id, cart_json) VALUES (%s, %s)",
                    (C1, json.dumps(cart)))
        self.db.commit()
        self.assertEqual(self.fc.count_references(self.db, C1, self.mother["id"]), 1)
        self.assertEqual(self.fc.release_profile(self.db, C1, self.mother["id"]), 1)
        persisted = self._persisted_cart()
        self.assertEqual(persisted[0][self.fc.LINE_KEY], 0)
        self.assertEqual(self._commercial(persisted[0]), self._commercial(cart[0]))
        self.assertEqual(self.fc.count_references(self.db, C1, self.mother["id"]), 0)

    def test_a_line_whose_person_is_gone_reads_as_nobody(self):
        cart = copy.deepcopy(self.cart)
        cart[0][self.fc.LINE_KEY] = self.mother["id"]
        self.fp.delete_profile(self.db, C1, self.mother["id"])
        view = self.fc.decorate(self.db, C1, cart)
        self.assertEqual(view["lines"][0]["profile_id"], 0)
        self.assertEqual(view["lines"][0]["fit"]["classification"], "no_person")
        with self.app.test_request_context():
            from flask import session
            session["user_id"] = C1
            self.assertTrue(self.fc.default_lines(self.db, C1, session, cart))
            self.assertEqual(cart[0][self.fc.LINE_KEY], 0)


if __name__ == "__main__":
    unittest.main()
