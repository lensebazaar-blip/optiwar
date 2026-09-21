"""FACE-C3 — server-side Favorites with an optional person per frame.

A signed-in customer's favourites live in customer_favorites, one row per
(customer, product); the browser's localStorage list is folded in additively
and never trimmed. A spectacle frame may be saved for one of the customer's
own people or for nobody; a lens never carries a person. Deleting a person
keeps the favourite and resets it to nobody.
"""
import importlib.util
import os
import sys
import unittest

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_face_profiles import (  # noqa: E402
    AVAILABLE, C1, C2, LEGACY_DDL, _connect, _load_api, _load_service, _wipe,
)
from test_face_fit import MEAS, PRODUCTS_DDL  # noqa: E402


def _load_favorites_api(fp, db_factory):
    """face_profiles_api + favorites_api under one throwaway package."""
    api = _load_api(fp, db_factory)
    pkg_name = "fp_pkg"
    spec = importlib.util.spec_from_file_location(
        pkg_name + ".favorites_api", os.path.join(REPO, "favorites_api.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return api, mod


class SchemaTests(unittest.TestCase):

    def test_one_row_per_customer_and_product_with_an_optional_person(self):
        pkg_name = "fav_schema_pkg"
        import types
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [REPO]
        sys.modules[pkg_name] = pkg
        sys.modules[pkg_name + ".face_profiles"] = _load_service()
        for name in ("lens_cart", "face_fit", "face_cart", "favorites"):
            spec = importlib.util.spec_from_file_location(
                pkg_name + "." + name, os.path.join(REPO, name + ".py"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
        fav = mod
        self.assertEqual([n for n, _ in fav.TABLES], ["customer_favorites"])
        self.assertIn("UNIQUE KEY uq_customer_favorite (customer_id, product_id)", fav.SCHEMA)
        self.assertIn("face_profile_id BIGINT UNSIGNED NULL", fav.SCHEMA)
        self.assertEqual(fav._product_ids(["12", 12, "x", "-1", "0", None, "7"]), [12, 7])


@unittest.skipUnless(AVAILABLE, "MariaDB test database not reachable")
class FavoritesTests(unittest.TestCase):

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
        cur.execute("DROP TABLE IF EXISTS customer_favorites")
        cls.db.commit()
        cls.fp.ensure_schema(cls.db)
        cls.api, cls.fav_api = _load_favorites_api(cls.fp, lambda: cls.db)
        cls.fav = sys.modules["fp_pkg.favorites"]
        cls.fav.ensure_schema(cls.db)
        cls.fav.ensure_schema(cls.db)  # idempotent
        cls.app = Flask(__name__)
        cls.app.config.update(TESTING=True, SECRET_KEY="test",
                              FACE_PROFILES_ENABLED="1",
                              FACE_PROFILES_ALLOW_EMAILS="lensebazaar@gmail.com")
        bp = Blueprint("main", __name__)
        cls.api.register(bp)
        cls.fav_api.register(bp)
        cls.app.register_blueprint(bp)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        _wipe(cls.db, C1, C2)
        cur = cls.db.cursor()
        cur.execute("DELETE FROM customer_favorites WHERE customer_id IN (%s, %s)", (C1, C2))
        cur.execute("DROP TABLE IF EXISTS products")
        cls.db.commit()
        cls.db.close()

    def setUp(self):
        _wipe(self.db, C1, C2)
        cur = self.db.cursor()
        cur.execute("DELETE FROM customer_favorites WHERE customer_id IN (%s, %s)", (C1, C2))
        self.db.commit()
        self.me = self.fp.ensure_self(self.db, C1, "Sudhanshu")
        self.mother = self.fp.create_profile(self.db, C1, "Mother", "parent", consent=True)
        self.fp.record_scan(self.db, C1, self.mother["id"], MEAS)
        self.stranger = self.fp.ensure_self(self.db, C2, "Other")
        self._login(C1, "lensebazaar@gmail.com", "Sudhanshu")

    def _login(self, cid, email, name):
        with self.client.session_transaction() as s:
            s.clear()
            s["user_id"] = cid
            s["user_email"] = email
            s["user_name"] = name

    def _rows(self, cid=C1):
        cur = self.db.cursor()
        cur.execute("SELECT product_id, face_profile_id FROM customer_favorites "
                    "WHERE customer_id=%s ORDER BY product_id", (cid,))
        return {r["product_id"]: r["face_profile_id"] for r in cur.fetchall()}

    def _shop_for(self, pid):
        r = self.client.post("/api/face-context", json={"face_profile_id": pid})
        self.assertEqual(r.status_code, 200, r.get_json())

    # -- access -----------------------------------------------------------

    def test_anonymous_gets_401_everywhere(self):
        with self.client.session_transaction() as s:
            s.clear()
        self.assertEqual(self.client.get("/api/favorites").status_code, 401)
        self.assertEqual(self.client.post("/api/favorites", json={"product_id": 9800101}).status_code, 401)
        self.assertEqual(self.client.post("/api/favorites/sync", json={"product_ids": [9800101]}).status_code, 401)
        self.assertEqual(self.client.delete("/api/favorites/9800101").status_code, 401)
        self.assertEqual(self.client.post("/api/favorites/9800101/person",
                                          json={"face_profile_id": 0}).status_code, 401)
        self.assertEqual(self._rows(), {})

    # -- saving -----------------------------------------------------------

    def test_a_frame_is_saved_for_the_person_being_shopped_for(self):
        self._shop_for(self.mother["id"])
        r = self.client.post("/api/favorites", json={"product_id": 9800101})
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()["favorite"]
        self.assertEqual(j["face_profile_id"], self.mother["id"])
        self.assertEqual(j["person"]["display_name"], "Mother")
        self.assertEqual(j["fit"]["classification"], "excellent")
        self.assertEqual(self._rows(), {9800101: self.mother["id"]})

    def test_a_lens_is_saved_for_nobody_whoever_is_being_shopped_for(self):
        self._shop_for(self.mother["id"])
        r = self.client.post("/api/favorites", json={"product_id": 9800103})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["favorite"]["face_profile_id"], 0)
        self.assertIsNone(r.get_json()["favorite"]["fit"])
        self.assertEqual(self._rows(), {9800103: None})

    def test_saving_twice_is_one_row_and_keeps_the_person(self):
        self._shop_for(self.mother["id"])
        self.client.post("/api/favorites", json={"product_id": 9800101})
        self._shop_for(0)
        r = self.client.post("/api/favorites", json={"product_id": 9800101})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._rows(), {9800101: self.mother["id"]})
        self.assertEqual(len(self.client.get("/api/favorites").get_json()["favorites"]), 1)

    def test_explicit_zero_saves_for_nobody_and_a_strangers_person_is_404(self):
        self._shop_for(self.mother["id"])
        r = self.client.post("/api/favorites", json={"product_id": 9800101, "face_profile_id": 0})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._rows(), {9800101: None})
        r = self.client.post("/api/favorites",
                             json={"product_id": 9800102, "face_profile_id": self.stranger["id"]})
        self.assertEqual(r.status_code, 404)
        self.assertNotIn(9800102, self._rows())
        r = self.client.post("/api/favorites", json={"product_id": 9800102, "face_profile_id": "x"})
        self.assertEqual(r.status_code, 400)

    def test_an_unknown_product_or_bad_id_is_refused(self):
        self.assertEqual(self.client.post("/api/favorites", json={"product_id": 4242}).status_code, 404)
        self.assertEqual(self.client.post("/api/favorites", json={"product_id": "abc"}).status_code, 400)
        self.assertEqual(self._rows(), {})

    def test_without_the_face_feature_a_favorite_has_no_person(self):
        self._login(C2, "someone@example.com", "Other")
        r = self.client.post("/api/favorites",
                             json={"product_id": 9800101, "face_profile_id": self.stranger["id"]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["favorite"]["face_profile_id"], 0)
        self.assertNotIn("fit", r.get_json()["favorite"])
        self.assertEqual(self._rows(C2), {9800101: None})
        r = self.client.post("/api/favorites/9800101/person", json={"face_profile_id": 0})
        self.assertEqual(r.status_code, 404)

    # -- the browser's list -----------------------------------------------

    def test_sync_folds_the_browsers_list_in_and_never_removes(self):
        self._shop_for(self.mother["id"])
        self.client.post("/api/favorites", json={"product_id": 9800101})
        r = self.client.post("/api/favorites/sync",
                             json={"product_ids": ["9800102", "9800103", "junk", "9800101", 4242]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["added"], 2)
        self.assertEqual(self._rows(), {9800101: self.mother["id"], 9800102: None, 9800103: None})
        r = self.client.post("/api/favorites/sync", json={"product_ids": []})
        self.assertEqual(r.get_json()["added"], 0)
        self.assertEqual(sorted(f["product_id"] for f in r.get_json()["favorites"]),
                         [9800101, 9800102, 9800103])
        self.assertEqual(self._rows(), {9800101: self.mother["id"], 9800102: None, 9800103: None})

    def test_a_removed_favorite_is_gone_and_removing_again_is_harmless(self):
        self.client.post("/api/favorites", json={"product_id": 9800101})
        r = self.client.delete("/api/favorites/9800101")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["removed"])
        self.assertEqual(self._rows(), {})
        r = self.client.delete("/api/favorites/9800101")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["removed"])

    # -- reassigning ------------------------------------------------------

    def test_reassigning_changes_the_person_and_the_fit_only(self):
        self._shop_for(0)
        self.client.post("/api/favorites", json={"product_id": 9800101})
        r = self.client.post("/api/favorites/9800101/person",
                             json={"face_profile_id": self.mother["id"]})
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertEqual(j["fit"]["classification"], "excellent")
        self.assertEqual(j["favorite"]["person"]["display_name"], "Mother")
        self.assertEqual(self._rows(), {9800101: self.mother["id"]})
        r = self.client.post("/api/favorites/9800101/person", json={"face_profile_id": 0})
        self.assertEqual(r.get_json()["fit"]["classification"], "no_person")
        self.assertEqual(self._rows(), {9800101: None})
        r = self.client.post("/api/favorites/9800101/person",
                             json={"face_profile_id": self.me["id"]})
        self.assertEqual(r.get_json()["fit"]["classification"], "no_measurement")

    def test_a_lens_or_unsaved_or_foreign_assignment_is_refused(self):
        self.client.post("/api/favorites", json={"product_id": 9800103})
        r = self.client.post("/api/favorites/9800103/person",
                             json={"face_profile_id": self.mother["id"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "not_a_frame")
        r = self.client.post("/api/favorites/9800101/person",
                             json={"face_profile_id": self.mother["id"]})
        self.assertEqual(r.status_code, 404)
        self.client.post("/api/favorites", json={"product_id": 9800101, "face_profile_id": 0})
        r = self.client.post("/api/favorites/9800101/person",
                             json={"face_profile_id": self.stranger["id"]})
        self.assertEqual(r.status_code, 404)
        r = self.client.post("/api/favorites/9800101/person", json={"face_profile_id": -3})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._rows(), {9800101: None, 9800103: None})

    # -- deleting a person ------------------------------------------------

    def test_deleting_a_person_keeps_the_favorites_and_resets_them_to_nobody(self):
        self._shop_for(self.mother["id"])
        self.client.post("/api/favorites", json={"product_id": 9800101})
        self.client.post("/api/favorites", json={"product_id": 9800102})
        self.client.post("/api/favorites", json={"product_id": 9800103})
        r = self.client.get("/api/face-profiles/%d/references" % self.mother["id"])
        self.assertEqual(r.get_json()["references"]["favorites"], 2)
        r = self.client.delete("/api/face-profiles/%d" % self.mother["id"])
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["favorites_reset"], 2)
        self.assertEqual(self._rows(), {9800101: None, 9800102: None, 9800103: None})
        j = self.client.get("/api/favorites").get_json()
        self.assertEqual(sorted(f["product_id"] for f in j["favorites"]), [9800101, 9800102, 9800103])
        self.assertTrue(all(f["face_profile_id"] == 0 for f in j["favorites"]))

    def test_the_favorites_page_lines_cover_frames_only_and_list_the_people(self):
        self._shop_for(self.mother["id"])
        self.client.post("/api/favorites", json={"product_id": 9800101})
        self.client.post("/api/favorites", json={"product_id": 9800103})
        cur = self.db.cursor()
        cur.execute("SELECT * FROM products WHERE product_id IN (9800101, 9800103)")
        products = cur.fetchall()
        ctx = self.fav.decorate(self.db, C1, products, True)
        self.assertEqual(set(ctx["lines"]), {9800101})
        self.assertEqual(ctx["lines"][9800101]["profile_id"], self.mother["id"])
        self.assertEqual(ctx["lines"][9800101]["fit"]["classification"], "excellent")
        self.assertEqual([p["display_name"] for p in ctx["people"]], ["Sudhanshu", "Mother"])
        self.assertEqual(ctx["nobody_label"], "No person / Gift")
        self.assertIsNone(self.fav.decorate(self.db, C1, products, False))
        html = self._render(products, ctx)
        self.assertIn('Saved for', html)
        self.assertEqual(html.count('class="ow-fav-face"'), 1)
        self.assertIn('<option value="%d" selected>Mother (Parent)</option>' % self.mother["id"], html)
        self.assertIn('<option value="0">No person / Gift</option>', html)
        self.assertIn('Fit: ', html)
        self.assertNotIn('Face fit: ', html)
        html = self._render(products, None)
        self.assertNotIn('class="ow-fav-face"', html)
        self.assertNotIn('/api/favorites/', html)

    def _render(self, products, face_lines):
        env = Environment(
            loader=ChoiceLoader([
                DictLoader({"base.html": "{% block title %}{% endblock %}{% block content %}{% endblock %}"}),
                FileSystemLoader(os.path.join(REPO, "templates"))]),
            autoescape=select_autoescape(["html"]))
        env.globals["url_for"] = lambda name, **kw: "/" + name
        env.globals["img_has_derivatives"] = lambda *a, **k: False
        env.globals["img_ver"] = lambda *a, **k: ""
        for p in products:
            p.setdefault("product_image", "")
            p.setdefault("product_perception_value", "")
        return env.get_template("favorites.html").render(
            products=products, face_lines=face_lines, is_india=True,
            favorite_ids=[p["product_id"] for p in products])


if __name__ == "__main__":
    unittest.main()
