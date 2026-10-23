"""Ops' read-only order face-scan view, with isolated MariaDB fixtures."""

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
import uuid
from datetime import datetime, timedelta

from flask import Blueprint, Flask

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_face_profiles import LEGACY_DDL  # noqa: E402
from test_face_fit import MEAS, PRODUCTS_DDL  # noqa: E402

DB_CONF = dict(
    host=os.environ.get("OPTIWAR_TEST_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("OPTIWAR_TEST_MYSQL_PORT", "3306")),
    user=os.environ.get("OPTIWAR_TEST_MYSQL_USER", "oslb6"),
    password=os.environ.get("OPTIWAR_TEST_MYSQL_PASSWORD", "testpw"),
    database=os.environ.get("OPTIWAR_TEST_MYSQL_DB", "optiwar2_test_ofs"),
)

CUSTOMER = 98271001
OTHER_CUSTOMER = 98271002
FRAME_ID = 419
LENS_ID = 98271003


def _connect():
    import pymysql
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor,
                           autocommit=False, connect_timeout=5, **DB_CONF)


def _available():
    try:
        db = _connect()
        db.close()
        return True
    except Exception:  # noqa: BLE001 - skip the integration fixture if unavailable
        return False


AVAILABLE = _available()


def _load_file(package, name):
    spec = importlib.util.spec_from_file_location(
        package + "." + name, os.path.join(REPO, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_service():
    package_name = "order_face_scan_under_test"
    package = types.ModuleType(package_name)
    package.__path__ = [REPO]
    sys.modules[package_name] = package

    profiles = _load_file(package_name, "face_profiles")
    _load_file(package_name, "face_fit")
    _load_file(package_name, "lens_cart")
    _load_file(package_name, "face_cart")

    state = {"authorized": True}
    db_module = types.ModuleType(package_name + ".db")
    db_module.get_db = lambda: state["db"]
    sys.modules[db_module.__name__] = db_module
    ops_module = types.ModuleType(package_name + ".ops")
    ops_module._require_ops_auth = lambda: state["authorized"]
    sys.modules[ops_module.__name__] = ops_module
    service = _load_file(package_name, "order_face_scan")
    service._test_state = state
    service._test_profiles = profiles
    service._test_fit = sys.modules[package_name + ".face_fit"]
    return service


@unittest.skipUnless(AVAILABLE, "MariaDB test database not reachable")
class OrderFaceScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = _connect()
        cls.service = _load_service()
        cls.fp = cls.service._test_profiles
        cls.fit = cls.service._test_fit
        cur = cls.db.cursor()
        cur.execute(LEGACY_DDL)
        cur.execute(PRODUCTS_DDL)
        cur.execute(cls.fp.PROFILES_SCHEMA)
        cur.execute(cls.fp.SCANS_SCHEMA)
        cur.execute(
            """CREATE TABLE IF NOT EXISTS order_face_snapshots (
                 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                 order_id VARCHAR(64) NOT NULL, line_no INT NOT NULL,
                 product_id INT NOT NULL, customer_id INT NOT NULL,
                 face_profile_id BIGINT UNSIGNED NULL, display_name VARCHAR(80) NULL,
                 relationship_type VARCHAR(32) NULL, is_self TINYINT NOT NULL DEFAULT 0,
                 face_scan_id BIGINT UNSIGNED NULL, measured_at DATETIME NULL,
                 measurements TEXT NULL, product_size VARCHAR(40) NULL,
                 fit_classification VARCHAR(20) NOT NULL, fit_label VARCHAR(40) NOT NULL,
                 fit_json TEXT NULL, snapshot_at DATETIME NOT NULL,
                 UNIQUE KEY uq_order_face_line (order_id, line_no)
             ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                 order_line_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                 order_id VARCHAR(64) NOT NULL, date_created DATETIME NULL,
                 customer_id INT NULL, product_id INT NULL, site_from VARCHAR(64) NULL,
                 KEY idx_order (order_id)
             ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cls.db.commit()
        cur.close()

        cls.root_parent = tempfile.mkdtemp(prefix="order-face-scan-")
        cls.app_root = os.path.join(cls.root_parent, "flaskr")
        os.makedirs(cls.app_root)
        cls.app = Flask(__name__, root_path=cls.app_root,
                        template_folder=os.path.join(REPO, "templates"))
        cls.app.config.update(TESTING=True, SECRET_KEY="test",
                              FACE_RAW_CAPTURE_RETENTION_DAYS=7)
        blueprint = Blueprint("main", __name__)
        cls.service.register(blueprint)
        cls.app.register_blueprint(blueprint)
        cls.client = cls.app.test_client()
        cls.service._test_state["db"] = cls.db

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        self.order_ids = []
        self.file_names = []
        self.customer_id = CUSTOMER + (id(self) % 100000)
        self.customer_ids = [self.customer_id]
        self.service._test_state["authorized"] = True
        self._product(FRAME_ID, "BO74", "Spectacles Frame", "54-17-140",
                      "Spectacles Frame")

    def tearDown(self):
        cur = self.db.cursor()
        for order_id in self.order_ids:
            cur.execute("DELETE FROM order_face_snapshots WHERE order_id=%s",
                        (order_id,))
            cur.execute("DELETE FROM orders WHERE order_id=%s", (order_id,))
        placeholders = ",".join(["%s"] * len(self.customer_ids))
        cur.execute("DELETE FROM face_measurements WHERE customer_id IN (%s)" % placeholders,
                    tuple(self.customer_ids))
        cur.execute("DELETE FROM face_scans WHERE customer_id IN (%s,%s)" % (
            placeholders, "%s"), tuple(self.customer_ids + [OTHER_CUSTOMER]))
        cur.execute("DELETE FROM face_profiles WHERE customer_id IN (%s,%s)" % (
            placeholders, "%s"), tuple(self.customer_ids + [OTHER_CUSTOMER]))
        cur.execute("DELETE FROM products WHERE product_id IN (%s,%s)",
                    (FRAME_ID, LENS_ID))
        self.db.commit()
        cur.close()
        for name in self.file_names:
            try:
                os.remove(os.path.join(self.fp.capture_dir(self.app_root), name))
            except OSError:
                pass

    def _new_customer(self):
        customer_id = self.customer_id + len(self.customer_ids)
        self.customer_ids.append(customer_id)
        return customer_id

    def _product(self, product_id, code, category, size, name):
        cur = self.db.cursor()
        cur.execute(
            """INSERT INTO products
               (product_id, product_code, product_name, product_category, product_size)
               VALUES (%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE product_code=VALUES(product_code),
                 product_name=VALUES(product_name), product_category=VALUES(product_category),
                 product_size=VALUES(product_size)""",
            (product_id, code, name, category, size))
        self.db.commit()
        cur.close()

    def _order(self, lines=None, order_date=None, customer_id=None):
        order_id = "OFS-%s" % uuid.uuid4().hex[:12].upper()
        self.order_ids.append(order_id)
        order_date = order_date or datetime.now().replace(microsecond=0)
        customer_id = customer_id if customer_id is not None else self.customer_id
        cur = self.db.cursor()
        for product_id in lines or [FRAME_ID]:
            cur.execute(
                "INSERT INTO orders (order_id, date_created, customer_id, product_id, site_from) "
                "VALUES (%s,%s,%s,%s,%s)",
                (order_id, order_date, customer_id, product_id, "optiwar.in"))
        self.db.commit()
        cur.close()
        return order_id, order_date

    def _legacy_scan(self, measured_at, values=None, screenshot_path=None,
                     customer_id=None):
        values = dict(values or {
            "pd_far": 65, "pd_near": 62, "face_width": 142.5,
            "eye_mouth": 70, "recommended_diameter": 53,
            "recommended_bridge": 24, "recommended_length": 145,
            "decentration": 1.5,
        })
        customer_id = customer_id if customer_id is not None else self.customer_id
        cur = self.db.cursor()
        cur.execute(
            """INSERT INTO face_measurements
               (customer_id, pd_far, pd_near, face_width, eye_mouth,
                recommended_diameter, recommended_bridge, recommended_length,
                decentration, screenshot_path, measured_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (customer_id, values.get("pd_far"), values.get("pd_near"),
             values.get("face_width"), values.get("eye_mouth"),
             values.get("recommended_diameter"), values.get("recommended_bridge"),
             values.get("recommended_length"), values.get("decentration"),
             screenshot_path, measured_at))
        self.db.commit()
        cur.close()
        return values

    def _capture_file(self, name, data=b"\xff\xd8\xffimage"):
        directory = self.fp.capture_dir(self.app_root)
        with open(os.path.join(directory, name), "wb") as image:
            image.write(data)
        self.file_names.append(name)

    def test_legacy_scan_before_order_returns_fit_and_pd_warning(self):
        order_id, placed = self._order(order_date=datetime(2026, 8, 1, 12, 0))
        row = dict(pd_far=65, pd_near=62, face_width=142.5, eye_mouth=70,
                   recommended_diameter=53, recommended_bridge=24,
                   recommended_length=145, decentration=1.5,
                   measured_at=placed - timedelta(hours=1))
        self._legacy_scan(row["measured_at"], row)

        result = self.service.scan_for_order(self.db, order_id, self.app_root)
        line = result["lines"][0]
        expected_fit = self.fit.evaluate(row, "54-17-140")
        self.assertEqual(result["status"], "SCAN_FOUND")
        self.assertEqual(line["source"], "account_scan")
        self.assertTrue(line["scan"]["scanned_before_order"])
        self.assertEqual(line["scan"]["hours_from_order"], -1.0)
        self.assertEqual(line["fit"], expected_fit)
        self.assertEqual(line["fit"]["classification"], "very_good")
        self.assertEqual(line["fit"]["label"], "VERY GOOD")
        self.assertIn(self.service.BINOCULAR_WARNING, result["warnings"])

    def test_order_prefix_and_case_are_normalised(self):
        order_id, _placed = self._order()
        normal = self.service.scan_for_order(self.db, order_id, self.app_root)
        prefixed = self.service.scan_for_order(
            self.db, " ow-" + order_id.lower(), self.app_root)
        self.assertEqual(prefixed, normal)

    def test_no_scan_still_lists_lines_and_asks_customer_to_scan(self):
        order_id, _placed = self._order()
        result = self.service.scan_for_order(self.db, order_id, self.app_root)
        self.assertEqual(result["status"], "NO_SCAN")
        self.assertEqual(len(result["lines"]), 1)
        self.assertIn(self.service.NO_SCAN_WARNING, result["warnings"])

    def test_sealed_snapshot_wins_over_a_different_live_scan(self):
        order_id, placed = self._order()
        live = dict(pd_far=71, pd_near=67, face_width=151,
                    recommended_diameter=58, recommended_bridge=22,
                    recommended_length=160, measured_at=placed + timedelta(days=1))
        self._legacy_scan(live["measured_at"], live)
        sealed_measurements = {
            "pd_far": 63.0, "pd_near": 60.0, "face_width": 132.0,
            "eye_mouth": 70.0, "recommended_diameter": 52,
            "recommended_bridge": 18, "recommended_length": 140,
            "decentration": 2.0,
        }
        sealed_fit = self.fit.evaluate(sealed_measurements, "54-17-140")
        cur = self.db.cursor()
        cur.execute(
            """INSERT INTO order_face_snapshots
               (order_id,line_no,product_id,customer_id,face_profile_id,display_name,
                face_scan_id,measured_at,measurements,product_size,fit_classification,
                fit_label,fit_json,snapshot_at)
               VALUES (%s,1,%s,%s,7101,'Mira',NULL,%s,%s,%s,%s,%s,%s,%s)""",
            (order_id, FRAME_ID, self.customer_id, placed - timedelta(days=1),
             json.dumps(sealed_measurements), "54-17-140",
             sealed_fit["classification"], sealed_fit["label"],
             json.dumps(sealed_fit), placed))
        self.db.commit()
        cur.close()

        result = self.service.scan_for_order(self.db, order_id, self.app_root)
        line = result["lines"][0]
        self.assertEqual(line["source"], "order_snapshot")
        self.assertEqual(line["person"], "Mira")
        self.assertEqual(line["scan"]["pd_far"], 63.0)
        self.assertEqual(line["scan"]["measured_at"],
                         (placed - timedelta(days=1)).isoformat())
        self.assertEqual(line["fit"], sealed_fit)

    def test_scan_after_order_adds_warning(self):
        order_id, placed = self._order()
        self._legacy_scan(placed + timedelta(minutes=30))
        result = self.service.scan_for_order(self.db, order_id, self.app_root)
        self.assertIn("Scan taken after the order was placed.", result["warnings"])

    def test_capture_download_retention_ownership_and_basename_safety(self):
        image_name = "present.jpg"
        self._capture_file(image_name)
        order_id, placed = self._order()
        self._legacy_scan(placed - timedelta(hours=1), screenshot_path=image_name)
        with self.app.test_request_context():
            result = self.service.scan_for_order(self.db, order_id, self.app_root)
        response = self.client.get(result["lines"][0]["scan"]["capture"]["url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

        old_customer = self._new_customer()
        old_order, _old_placed = self._order(customer_id=old_customer)
        self._legacy_scan(datetime.now() - timedelta(days=10),
                          screenshot_path="deleted.jpg",
                          customer_id=old_customer)
        old_result = self.service.scan_for_order(self.db, old_order, self.app_root)
        self.assertFalse(old_result["lines"][0]["scan"]["capture"]["available"])
        self.assertEqual(old_result["lines"][0]["scan"]["capture"]["reason"],
                         "purged_after_retention")
        self.assertEqual(self.client.get(
            "/ops/api/orders/%s/face-scan/capture" % old_order).status_code, 404)

        foreign_name = "foreign.jpg"
        self._capture_file(foreign_name)
        cur = self.db.cursor()
        cur.execute(
            """INSERT INTO face_scans
               (customer_id,status,capture_path,measured_at)
               VALUES (%s,'COMPLETED',%s,%s)""",
            (OTHER_CUSTOMER, foreign_name, datetime.now()))
        foreign_scan_id = cur.lastrowid
        self.db.commit()
        cur.close()
        self.assertEqual(self.client.get(
            "/ops/api/orders/%s/face-scan/capture?scan_id=%d"
            % (order_id, foreign_scan_id)).status_code, 404)

        traversal_customer = self._new_customer()
        traversal_order, traversal_date = self._order(customer_id=traversal_customer)
        self._legacy_scan(traversal_date - timedelta(hours=1),
                          screenshot_path="../../etc/passwd",
                          customer_id=traversal_customer)
        traversal_result = self.service.scan_for_order(
            self.db, traversal_order, self.app_root)
        self.assertFalse(traversal_result["lines"][0]["scan"]["capture"]["available"])
        self.assertEqual(self.client.get(
            "/ops/api/orders/%s/face-scan/capture" % traversal_order).status_code, 404)

    def test_auth_unknown_order_and_authed_routes(self):
        order_id, _placed = self._order()
        self.service._test_state["authorized"] = False
        for path in ("/ops/api/orders/%s/face-scan" % order_id,
                     "/ops/api/orders/%s/face-scan/capture" % order_id,
                     "/ops/face-scan?order=%s" % order_id):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.service._test_state["authorized"] = True
        self.assertEqual(self.client.get(
            "/ops/api/orders/%s/face-scan" % order_id).status_code, 200)
        self.assertEqual(self.client.get(
            "/ops/face-scan?order=%s" % order_id).status_code, 200)
        missing = self.client.get("/ops/api/orders/NO-SUCH-ORDER/face-scan")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json(), {"ok": False, "error": "not_found"})

    def test_lens_line_has_no_fit(self):
        self._product(LENS_ID, "LENS1", "Contact Lenses", "", "Daily lens")
        order_id, _placed = self._order(lines=[LENS_ID])
        result = self.service.scan_for_order(self.db, order_id, self.app_root)
        self.assertFalse(result["lines"][0]["is_frame"])
        self.assertIsNone(result["lines"][0]["fit"])

    def test_people_lists_latest_completed_profile_scan(self):
        order_id, _placed = self._order()
        cur = self.db.cursor()
        cur.execute(
            """INSERT INTO face_profiles
               (customer_id,display_name,relationship_type,is_active)
               VALUES (%s,'Family member','sibling',1)""",
            (self.customer_id,))
        profile_id = cur.lastrowid
        cur.execute(
            """INSERT INTO face_scans
               (customer_id,face_profile_id,status,pd_far,pd_near,face_width,
                recommended_diameter,recommended_bridge,recommended_length,measured_at)
               VALUES (%s,%s,'COMPLETED',64,61,138,52,18,140,%s)""",
            (self.customer_id, profile_id, datetime.now() - timedelta(hours=2)))
        scan_id = cur.lastrowid
        self.db.commit()
        cur.close()

        result = self.service.scan_for_order(self.db, order_id, self.app_root)
        self.assertEqual(result["people"][0]["display_name"], "Family member")
        self.assertEqual(result["people"][0]["scan_id"], scan_id)
        self.assertEqual(result["people"][0]["pd_far"], 64)
        self.assertEqual(result["people"][0]["recommended_size"], "52-18-140")

    def test_html_shows_values_warnings_and_escapes_product_name(self):
        unsafe_name = "<script>alert(1)</script>"
        self._product(FRAME_ID, "BO74", "Spectacles Frame", "54-17-140",
                      unsafe_name)
        order_id, placed = self._order()
        self._legacy_scan(placed - timedelta(hours=1))
        response = self.client.get("/ops/face-scan?order=%s" % order_id)
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(order_id, body)
        self.assertIn("65", body)
        self.assertIn("Scan PD is binocular (far/near).", body)
        self.assertNotIn(unsafe_name, body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
