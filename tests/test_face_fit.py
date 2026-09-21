"""The frame-fit engine and the person being shopped for.

One implementation of the fit rule serves every surface; here it is pinned
against the numbers the product page, the listings and the matching-frames
API have always produced, and the context/fit routes are checked for
ownership (a stranger's profile is 404) and for the "No person" state.
"""
import importlib.util
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_face_profiles import (  # noqa: E402
    AVAILABLE, C1, C2, LEGACY_DDL, _connect, _load_api, _load_service, _wipe,
)


def _load_engine(fp_mod):
    pkg_name = "ff_pkg"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [REPO]
    sys.modules[pkg_name] = pkg
    sys.modules[pkg_name + ".face_profiles"] = fp_mod
    spec = importlib.util.spec_from_file_location(
        pkg_name + ".face_fit", os.path.join(REPO, "face_fit.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


PRODUCTS_DDL = """
CREATE TABLE IF NOT EXISTS products (
    product_id       INT NOT NULL PRIMARY KEY,
    product_code     VARCHAR(40) NOT NULL,
    product_name     VARCHAR(200) NOT NULL,
    product_category VARCHAR(80) NOT NULL,
    product_size     VARCHAR(32) NULL,
    product_slug     VARCHAR(200) NULL,
    product_status   VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    product_quantity INT NOT NULL DEFAULT 0,
    show_in_listings TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB
"""

MEAS = {"pd_far": 63.0, "pd_near": 60.0, "face_width": 132.0,
        "recommended_diameter": 52, "recommended_bridge": 18,
        "recommended_length": 140}


def _legacy_verdict(meas, size):
    """The arithmetic as it stood inline on the product page before the
    engine existed — the oracle the engine must agree with."""
    pd, fw, rl = float(meas["pd_far"]), float(meas["face_width"]), int(meas["recommended_length"])
    d, b, l = (int(x) for x in size.split("-"))
    width_diff = abs((d * 2) + b + 10 - fw)
    dec = abs((d + b) - pd) / 2.0
    if not (width_diff <= 8 and dec <= 6 and abs(l - rl) <= 10):
        return "not_matched"
    if width_diff <= 3 and dec <= 4:
        return "EXCELLENT"
    if width_diff <= 5 and dec <= 5:
        return "VERY GOOD"
    return "GOOD"


class EngineTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fp = _load_service()
        cls.ff = _load_engine(cls.fp)

    def test_parse_size(self):
        self.assertEqual(self.ff.parse_size("52-18-140"), (52, 18, 140))
        self.assertEqual(self.ff.parse_size(" 52 - 18 - 140 "), (52, 18, 140))
        self.assertIsNone(self.ff.parse_size("52-18"))
        self.assertIsNone(self.ff.parse_size(None))
        self.assertIsNone(self.ff.parse_size("medium"))

    def test_agrees_with_the_inline_rule_over_the_whole_size_space(self):
        ff = self.ff
        for d in range(44, 60):
            for b in range(14, 24):
                for l in (125, 135, 140, 145, 155):
                    size = "%d-%d-%d" % (d, b, l)
                    want = _legacy_verdict(MEAS, size)
                    got = ff.evaluate(MEAS, size)
                    if want == "not_matched":
                        self.assertFalse(got["matched"], size)
                        self.assertEqual(got["classification"], ff.NOT_MATCHED)
                        self.assertTrue(got["reasons"], size)
                    else:
                        self.assertTrue(got["matched"], size)
                        self.assertEqual(got["label"], want, size)

    def test_excellent_result_shape(self):
        r = self.ff.evaluate(MEAS, "52-18-140")
        self.assertEqual(r["classification"], "excellent")
        self.assertTrue(r["matched"])
        self.assertEqual(r["actual_dimensions"],
                         {"diameter": 52, "bridge": 18, "length": 140,
                          "size": "52-18-140", "frame_width": 132})
        self.assertEqual(r["recommended_dimensions"]["size"], "52-18-140")
        self.assertEqual(r["measurement_delta"],
                         {"frame_width_mm": 0.0, "decentration_mm": 3.5, "temple_mm": 0.0})
        self.assertEqual(r["measurements"]["recommended_size"], "52-18-140")

    def test_reasons_name_what_failed(self):
        r = self.ff.evaluate(MEAS, "58-22-160")
        self.assertEqual(r["classification"], "not_matched")
        self.assertIn("wider", " ".join(r["reasons"]))
        self.assertIn("lens centres", " ".join(r["reasons"]))
        self.assertIn("longer", " ".join(r["reasons"]))

    def test_no_person_no_measurement_no_size(self):
        ff = self.ff
        self.assertEqual(ff.evaluate(None, "52-18-140")["classification"], ff.NO_PERSON)
        self.assertEqual(ff.evaluate(None, "52-18-140")["label"], "Face fit not checked")
        self.assertEqual(ff.evaluate({"pd_far": None}, "52-18-140")["classification"],
                         ff.NO_MEASUREMENT)
        r = ff.evaluate(MEAS, "one size")
        self.assertEqual(r["classification"], ff.NO_SIZE)
        self.assertFalse(r["matched"])
        self.assertEqual(r["recommended_dimensions"]["size"], "52-18-140")

    def test_decimal_and_string_inputs_are_numbers(self):
        from decimal import Decimal
        m = dict(MEAS, pd_far=Decimal("63.0"), face_width="132")
        self.assertTrue(self.ff.evaluate(m, "52-18-140")["matched"])

    def test_matching_ids_and_legacy_api_labels(self):
        rows = [{"product_id": 1, "product_size": "52-18-140"},
                {"product_id": 2, "product_size": "58-22-160"},
                {"product_id": 3, "product_size": "n/a"}]
        self.assertEqual(self.ff.matching_product_ids(rows, MEAS), ["1"])
        self.assertEqual(self.ff.LEGACY_API_LABELS,
                         {"excellent": "Perfect", "very_good": "Good", "good": "Fair"})
        self.assertEqual(self.ff.score(self.ff.evaluate(MEAS, "52-18-140")), 7.0)

    def test_cache_scope_separates_people_and_scans(self):
        ff = self.ff
        self.assertEqual(ff.cache_scope(7, None, False), "7")
        self.assertEqual(ff.cache_scope(7, None, True), "7:nobody")
        a = ff.cache_scope(7, {"id": 3, "latest_scan_id": 10}, True)
        b = ff.cache_scope(7, {"id": 3, "latest_scan_id": 11}, True)
        c = ff.cache_scope(7, {"id": 4, "latest_scan_id": 10}, True)
        self.assertEqual(len({a, b, c}), 3)


@unittest.skipUnless(AVAILABLE, "MariaDB test database not reachable")
class ContextAndFitRouteTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Blueprint, Flask
        cls.fp = _load_service()
        cls.db = _connect()
        cur = cls.db.cursor()
        cur.execute(LEGACY_DDL)
        # Same cut-down products table the catalogue suites build and drop
        # (plus product_size, which the fit reads), so an earlier suite's
        # leftover shape cannot decide what this one sees.
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
        cls.db.commit()
        cls.fp.ensure_schema(cls.db)
        cls.api = _load_api(cls.fp, lambda: cls.db)
        cls.ff = sys.modules["fp_pkg.face_fit"]
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
        cur.execute("DROP TABLE IF EXISTS products")
        cls.db.commit()
        cls.db.close()

    def setUp(self):
        _wipe(self.db, C1, C2)
        self._login(C1, "lensebazaar@gmail.com", "Sudhanshu")
        self.me = self.fp.ensure_self(self.db, C1, "Sudhanshu")
        self.mother = self.fp.create_profile(self.db, C1, "Mother", "parent", consent=True)
        self.fp.record_scan(self.db, C1, self.mother["id"], MEAS)
        self.stranger = self.fp.ensure_self(self.db, C2, "Other")

    def _login(self, cid, email, name):
        with self.client.session_transaction() as s:
            s.clear()
            s["user_id"] = cid
            s["user_email"] = email
            s["user_name"] = name

    def test_context_defaults_to_the_default_profile_and_lists_everyone(self):
        r = self.client.get("/api/face-context")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["active_profile_id"], self.me["id"])
        self.assertEqual([p["display_name"] for p in j["profiles"]], ["Sudhanshu", "Mother"])
        self.assertFalse(j["explicit_nobody"])
        self.assertNotIn("capture_path", str(j))

    def test_switching_person_changes_the_fit_and_persists_in_the_session(self):
        r = self.client.get("/api/frames/9800101/fit")
        self.assertEqual(r.get_json()["fit"]["classification"], "no_measurement")
        r = self.client.post("/api/face-context",
                             json={"face_profile_id": self.mother["id"], "product_id": 9800101})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["active_profile_id"], self.mother["id"])
        self.assertEqual(j["fit"]["classification"], "excellent")
        self.assertEqual(j["fit"]["profile"]["display_name"], "Mother")
        self.assertEqual(j["fit"]["product"]["product_code"], "FIT1")
        r = self.client.get("/api/frames/9800102/fit")
        self.assertEqual(r.get_json()["fit"]["classification"], "not_matched")
        self.assertEqual(r.get_json()["fit"]["profile"]["id"], self.mother["id"])

    def test_no_person_is_an_explicit_state_with_no_fit(self):
        r = self.client.post("/api/face-context", json={"face_profile_id": None})
        j = r.get_json()
        self.assertIsNone(j["active_profile_id"])
        self.assertTrue(j["explicit_nobody"])
        r = self.client.get("/api/frames/9800101/fit")
        self.assertEqual(r.get_json()["fit"]["classification"], "no_person")
        self.assertEqual(r.get_json()["fit"]["label"], "Face fit not checked")
        self.assertIsNone(r.get_json()["fit"]["profile"])
        # ?face_profile_id=0 is the same request on the query string
        self.client.post("/api/face-context", json={"face_profile_id": self.me["id"]})
        r = self.client.get("/api/frames/9800101/fit?face_profile_id=0")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["fit"]["classification"], "no_person")

    def test_a_non_frame_is_not_fitted(self):
        self.assertEqual(self.client.get("/api/frames/9800103/fit").status_code, 404)

    def test_a_strangers_profile_is_404_everywhere(self):
        sid = self.stranger["id"]
        r = self.client.post("/api/face-context", json={"face_profile_id": sid})
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/api/frames/9800101/fit?face_profile_id=%d" % sid)
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/api/face-context")
        self.assertEqual(r.get_json()["active_profile_id"], self.me["id"])

    def test_a_deleted_choice_falls_back_to_the_default(self):
        self.client.post("/api/face-context", json={"face_profile_id": self.mother["id"]})
        self.fp.delete_profile(self.db, C1, self.mother["id"])
        r = self.client.get("/api/face-context")
        self.assertEqual(r.get_json()["active_profile_id"], self.me["id"])

    def test_unknown_product_is_404_and_gate_off_is_404(self):
        self.assertEqual(self.client.get("/api/frames/424242/fit").status_code, 404)
        self._login(C2, "someone@example.com", "Other")
        self.assertEqual(self.client.get("/api/face-context").status_code, 404)
        self.assertEqual(self.client.get("/api/frames/9800101/fit").status_code, 404)


if __name__ == "__main__":
    unittest.main()
