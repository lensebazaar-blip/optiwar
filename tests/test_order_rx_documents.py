import datetime
import importlib.util
import io
import json
import os
import random
import sys
import tempfile
import types
import unittest
from unittest import mock

from flask import Blueprint, Flask


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_CONF = {
    "host": os.environ.get("OPTIWAR_TEST_MYSQL_HOST", "127.0.0.1"),
    "user": os.environ.get("OPTIWAR_TEST_MYSQL_USER", "oslb6"),
    "password": os.environ.get("OPTIWAR_TEST_MYSQL_PASSWORD", "testpw"),
    "database": os.environ.get("OPTIWAR_TEST_MYSQL_DB", "optiwar2_test_ordrx"),
    "charset": "utf8mb4",
}


def _connect():
    import pymysql
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor,
                           autocommit=False, connect_timeout=5, **DB_CONF)


def _available():
    try:
        db = _connect()
        db.close()
        return True
    except Exception:
        return False


AVAILABLE = _available()
STATE = {"db": None, "authorized": True, "read": None, "defects": [],
         "persisted": [], "model_calls": []}


def _load_file(package, name):
    spec = importlib.util.spec_from_file_location(
        package + "." + name, os.path.join(REPO, name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    sys.modules[package].__dict__[name] = loaded
    return loaded


def _load_service():
    package_name = "order_rx_documents_under_test"
    package = types.ModuleType(package_name)
    package.__path__ = [REPO]
    sys.modules[package_name] = package

    db_module = types.ModuleType(package_name + ".db")
    db_module.get_db = lambda: STATE["db"]
    ops_module = types.ModuleType(package_name + ".ops")
    ops_module._require_ops_auth = lambda: STATE["authorized"]
    ai_module = types.ModuleType(package_name + ".ai_client")
    ai_module.ModelError = type("ModelError", (Exception,), {})

    def call_model(**kwargs):
        STATE["model_calls"].append(kwargs)
        proposal, confidence, model, unreadable = STATE["read"](
            [], kwargs["workload"], kwargs.get("endpoint"))
        payload = dict(proposal or {})
        payload["confidence"] = confidence
        if unreadable:
            payload["unreadable"] = True
        message = types.SimpleNamespace(content=json.dumps(payload))
        choice = types.SimpleNamespace(message=message)
        return types.SimpleNamespace(choices=[choice], model=model)

    ai_module.call_model = call_model
    defects_module = types.ModuleType(package_name + ".dev_defects")
    defects_module.record = lambda *args, **kwargs: STATE["defects"].append(
        (args, kwargs))
    models_module = types.ModuleType(package_name + ".models")
    models_module._persist_cart = lambda cart: STATE["persisted"].append(cart)
    for module in (db_module, ops_module, ai_module, defects_module,
                   models_module):
        sys.modules[module.__name__] = module
        setattr(package, module.__name__.split(".")[-1], module)

    lens_order = _load_file(package_name, "lens_order")
    lens_rx = _load_file(package_name, "lens_rx")
    lens_documents = _load_file(package_name, "lens_documents")
    upload_module = _load_file(package_name, "lens_upload")
    service = _load_file(package_name, "order_rx_documents")
    service._test_state = STATE
    service._test_lens_rx = lens_rx
    service._test_lens_documents = lens_documents
    service._test_lens_order = lens_order
    service._test_upload = upload_module
    return service


def _ensure_columns(cursor, table, columns):
    for column, definition in columns:
        cursor.execute(
            "ALTER TABLE `%s` ADD COLUMN IF NOT EXISTS `%s` %s"
            % (table, column, definition))


def _image_bytes(fmt="PNG", size=(180, 130), noise=False):
    from PIL import Image
    import random as _random
    image = Image.new("RGB", size, "white")
    if noise:
        pixels = image.load()
        rng = _random.Random(814)
        for y in range(size[1]):
            for x in range(size[0]):
                pixels[x, y] = tuple(rng.randrange(256) for _ in range(3))
    out = io.BytesIO()
    image.save(out, format=fmt)
    return out.getvalue()


class LensDocumentNormaliserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = _load_service()
        cls.documents = cls.service._test_lens_documents

    def test_noisy_jpeg_applies_exif_orientation_and_strips_metadata(self):
        from PIL import Image
        image = Image.new("RGB", (4000, 3000))
        pixels = image.load()
        rng = random.Random(12)
        for y in range(3000):
            for x in range(4000):
                pixels[x, y] = (rng.randrange(256), rng.randrange(256),
                                rng.randrange(256))
        exif = Image.Exif()
        exif[0x0112] = 6
        exif[0x013b] = "private metadata"
        from PIL.TiffImagePlugin import IFDRational
        exif[0x8825] = {
            1: "N", 2: (IFDRational(40, 1), IFDRational(30, 1),
                        IFDRational(0, 1)),
            3: "W", 4: (IFDRational(73, 1), IFDRational(59, 1),
                        IFDRational(0, 1)),
        }
        source = io.BytesIO()
        image.save(source, format="JPEG", quality=82, exif=exif)
        self.assertLess(len(source.getvalue()),
                        self.documents.MAX_UPLOAD_BYTES)
        result = self.documents.normalise(source.getvalue())
        self.assertLess(result["stored_bytes"], 2_000_000)
        self.assertEqual(result["stored_bytes"],
                         sum(map(len, result["pages"])))
        stored = Image.open(io.BytesIO(result["pages"][0]))
        self.assertGreater(stored.height, stored.width)
        self.assertLessEqual(max(stored.size), self.documents.MAX_SIDE_PX)
        self.assertFalse(stored.getexif())

    def test_phone_png_is_converted_to_bounded_jpeg(self):
        from PIL import Image
        source = _image_bytes("PNG", (1170, 2532))
        result = self.documents.normalise(source)
        self.assertEqual(result["kind"], self.documents.KIND_PNG)
        self.assertLess(result["stored_bytes"], 2_000_000)
        self.assertEqual(Image.open(io.BytesIO(result["pages"][0])).format,
                         "JPEG")

    def test_two_page_pdf_is_stored_under_total_budget(self):
        import fitz
        pdf = fitz.open()
        for label in ("right", "left"):
            page = pdf.new_page(width=612, height=792)
            page.insert_text((72, 90), "Prescription " + label)
        result = self.documents.normalise(pdf.tobytes())
        self.assertEqual(result["kind"], self.documents.KIND_PDF)
        self.assertEqual(len(result["pages"]), 2)
        self.assertLess(result["stored_bytes"], 2_000_000)

    def test_webp_is_accepted_and_stored_as_jpeg(self):
        from PIL import Image
        result = self.documents.normalise(
            _image_bytes("WEBP", (500, 500), noise=True))
        self.assertEqual(result["kind"], self.documents.KIND_WEBP)
        self.assertEqual(Image.open(io.BytesIO(result["pages"][0])).format,
                         "JPEG")

    def test_heic_without_optional_decoder_has_exact_refusal(self):
        data = b"\x00\x00\x00\x18ftypheic" + b"x" * 1400
        with mock.patch.dict(sys.modules, {"pillow_heif": None}):
            with self.assertRaises(self.documents.Rejected) as caught:
                self.documents.normalise(data)
        self.assertEqual(caught.exception.code, "RX_UPLOAD_HEIC_UNSUPPORTED")
        self.assertEqual(
            caught.exception.message,
            "iPhone HEIC photos can't be read here. Please take a screenshot "
            "of the photo and upload that, or upload it as JPEG.")

    def test_magic_mismatch_is_rejected(self):
        data = b"\xff\xd8\xff" + _image_bytes("PNG")
        with self.assertRaises(self.documents.Rejected) as caught:
            self.documents.normalise(data)
        self.assertEqual(caught.exception.code, "RX_UPLOAD_REJECTED")

    def test_tiny_oversized_and_corrupt_files_are_rejected(self):
        with self.assertRaises(self.documents.Rejected):
            self.documents.normalise(b"\xff\xd8\xff" + b"x" * 100)
        with self.assertRaises(self.documents.Rejected) as too_large:
            self.documents.normalise(
                b"\xff\xd8\xff" + b"x" *
                (self.documents.MAX_UPLOAD_BYTES + 1))
        self.assertEqual(too_large.exception.code, "RX_UPLOAD_REJECTED")
        with self.assertRaises(self.documents.Rejected):
            self.documents.normalise(b"\xff\xd8\xff" + b"x" * 1400)


@unittest.skipUnless(AVAILABLE, "MariaDB test database not reachable")
class OrderRxDocumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = _load_service()
        cls.lens_rx = cls.service._test_lens_rx
        cls.documents = cls.service._test_lens_documents
        cls.db = _connect()
        cursor = cls.db.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS products ("
            "product_id INT PRIMARY KEY, product_name VARCHAR(191) NULL, "
            "product_code VARCHAR(64) NULL, "
            "product_category VARCHAR(64) NULL, product_size VARCHAR(64) NULL)")
        _ensure_columns(cursor, "products", (
            ("product_id", "INT NULL"),
            ("product_name", "VARCHAR(191) NULL"),
            ("product_code", "VARCHAR(64) NULL"),
            ("product_category", "VARCHAR(64) NULL"),
            ("product_size", "VARCHAR(64) NULL"),
        ))
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS orders ("
            "order_line_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
            "order_id VARCHAR(32) NOT NULL,"
            "product_id INT NULL, rx_id INT NULL, customer_id INT NULL, "
            "date_created DATETIME NULL, site_from VARCHAR(64) NULL)")
        _ensure_columns(cursor, "orders", (
            ("order_line_id", "INT NULL"),
            ("order_id", "VARCHAR(32) NULL"),
            ("product_id", "INT NULL"),
            ("rx_id", "INT NULL"),
            ("customer_id", "INT NULL"),
            ("date_created", "DATETIME NULL"),
            ("site_from", "VARCHAR(64) NULL"),
            ("is_test", "TINYINT NULL DEFAULT 0"),
            ("archived", "TINYINT NULL DEFAULT 0"),
        ))
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS rx_collector ("
            "rx_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
            "recommendations TEXT NULL, recommendation_price INT NULL, "
            "right_eye VARCHAR(255) NULL, left_eye VARCHAR(255) NULL, "
            "product_id INT NULL, "
            "date_created DATETIME DEFAULT CURRENT_TIMESTAMP)")
        _ensure_columns(cursor, "rx_collector", (
            ("rx_id", "INT NULL"),
            ("recommendations", "TEXT NULL"),
            ("recommendation_price", "INT NULL"),
            ("right_eye", "VARCHAR(255) NULL"),
            ("left_eye", "VARCHAR(255) NULL"),
            ("product_id", "INT NULL"),
            ("date_created", "DATETIME NULL DEFAULT CURRENT_TIMESTAMP"),
        ))
        cursor.execute(
            "ALTER TABLE rx_collector MODIFY rx_id "
            "INT NOT NULL AUTO_INCREMENT")
        cls.service.ensure_schema(cursor)
        cursor.execute(
            "ALTER TABLE order_rx_documents MODIFY review_verdict "
            "VARCHAR(32) NULL")
        cls.db.commit()
        cls.db.close()

    def setUp(self):
        self.db = _connect()
        STATE["db"] = self.db
        STATE["authorized"] = True
        STATE["read"] = lambda pages, workload, endpoint: (
            self._proposal(), 0.96, "test-vision", False)
        STATE["model_calls"] = []
        STATE["defects"] = []
        STATE["persisted"] = []
        self.cid = random.randint(100_000_000, 1_800_000_000)
        self.pid = self.cid - 100
        self.line_id = self.cid - 200
        self.order_id = "RX%09d" % self.cid
        self.cart = [{
            "product_id": self.pid,
            "product_category": "Spectacles Frame",
            "right_pwr": "-1.00",
            "right_cyl": "-0.50",
            "right_axis": "180",
            "right_add": "1.50",
            "left_pwr": "-2.00",
            "left_cyl": "-0.25",
            "left_axis": "90",
            "left_add": "1.50",
            "rx_id": self.cid,
        }]
        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO products "
            "(product_id,product_name,product_code,product_category,"
            "product_size) VALUES (%s,%s,%s,%s,%s)",
            (self.pid, "Test spectacles", "RXTEST", "Spectacles Frame",
             "54-17-140"))
        cur.execute(
            "INSERT INTO orders "
            "(order_line_id,order_id,product_id,rx_id,customer_id) "
            "VALUES (%s,%s,%s,%s,%s)",
            (self.line_id, self.order_id, self.pid, self.cid, self.cid))
        self.db.commit()
        self.tmp = tempfile.TemporaryDirectory()
        self.app_root = os.path.join(self.tmp.name, "app")
        os.makedirs(self.app_root)
        self.app = Flask("rx_documents_tests",
                         root_path=self.app_root,
                         template_folder=os.path.join(REPO, "templates"))
        self.app.config.update(SECRET_KEY="rx-test-secret")
        bp = Blueprint("main", __name__)
        self.service.register(bp)
        self.app.register_blueprint(bp)
        self.client = self.app.test_client()
        self._set_session()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _set_session(self, *, customer=True, cart=None, email="ops@test.local"):
        with self.client.session_transaction() as sess:
            sess.clear()
            if customer:
                sess["user_id"] = self.cid
            if email:
                sess["user_email"] = email
            sess["cart"] = self.cart if cart is None else cart

    @staticmethod
    def _proposal():
        return {
            "right": {"sph": "-1.00", "cyl": "-0.50", "axis": "180",
                      "add": "1.50"},
            "left": {"sph": "-2.00", "cyl": "-0.25", "axis": "90",
                     "add": "1.50"},
        }

    def _upload(self, *, cart=None, product_id=None, line="0", content=None):
        self._set_session(cart=self.cart if cart is None else cart)
        return self.client.post(
            "/checkout/rx-upload",
            data={
                "line": line,
                "product_id": str(self.pid if product_id is None else product_id),
                "document": (io.BytesIO(content or _image_bytes(noise=True)),
                             "prescription.png"),
            },
            content_type="multipart/form-data")

    def _document(self, *, customer_id=None, product_id=None, status="PARSED",
                  created_at=None, order_id=None, pages=1):
        cursor = self.db.cursor()
        now = created_at or datetime.datetime.now()
        cursor.execute(
            "INSERT INTO order_rx_documents "
            "(customer_id,product_id,status,original_kind,original_bytes,"
            "stored_bytes,sha256,pages,typed_json,created_at,retain_until,"
            "order_id,attached_at) VALUES (%s,%s,%s,'jpeg',1400,1200,%s,%s,"
            "%s,%s,%s,%s,%s)",
            (customer_id or self.cid, product_id or self.pid, status,
             "a" * 64, pages, json.dumps(self.service.typed_snapshot(self.cart[0])),
             now, now.date() + datetime.timedelta(days=30),
             order_id, now if order_id else None))
        document_id = int(cursor.lastrowid)
        self.db.commit()
        return document_id

    def _write_file(self, document_id, page=0, content=None):
        with self.app.app_context():
            path = os.path.join(self.service.upload_dir(),
                                self.service.stored_name(document_id, page))
        with open(path, "wb") as image:
            image.write(content or _image_bytes("JPEG"))
        return path

    def _file_path(self, document_id, page=0):
        with self.app.app_context():
            return os.path.join(
                self.service.upload_dir(),
                self.service.stored_name(document_id, page))

    def _attach(self, document_id, *, cart=None):
        if cart is None:
            with self.client.session_transaction() as sess:
                cart = sess.get("cart", [])
        cursor = self.db.cursor()
        result = self.service.attach_for_order(
            cursor, self.order_id, self.cid, cart)
        self.db.commit()
        self.assertTrue(result)
        return cursor

    def _audit_actions(self, document_id):
        cur = self.db.cursor()
        cur.execute("SELECT action FROM order_rx_document_audit "
                    "WHERE document_id=%s ORDER BY id", (document_id,))
        return [row["action"] for row in cur.fetchall()]

    def test_upload_auth_line_and_product_validation(self):
        self._set_session(customer=False, email=None)
        unauth = self.client.post("/checkout/rx-upload")
        self.assertEqual(unauth.status_code, 401)
        self.assertEqual(unauth.json["message"],
                         "Please sign in again to add a photo.")
        self._set_session()
        bad_product = self._upload(product_id=self.pid + 99)
        self.assertEqual(bad_product.status_code, 400)
        self.assertEqual(bad_product.json["code"], "RX_UPLOAD_LINE")
        line_message = ("This frame has no prescription entered, so a photo "
                        "can't be added here.")
        self.assertEqual(bad_product.json["message"], line_message)
        no_rx = [dict(self.cart[0], right_pwr=None, left_pwr=None)]
        no_rx_response = self._upload(cart=no_rx)
        self.assertEqual(no_rx_response.json["code"], "RX_UPLOAD_LINE")
        self.assertEqual(no_rx_response.json["message"], line_message)
        lens = [dict(self.cart[0], product_category="Contact Lenses")]
        lens_response = self._upload(cart=lens)
        self.assertEqual(lens_response.json["code"], "RX_UPLOAD_LINE")
        self.assertEqual(lens_response.json["message"], line_message)
        bad_line = self._upload(line="5")
        self.assertEqual(bad_line.json["code"], "RX_UPLOAD_LINE")
        self.assertEqual(bad_line.json["message"], line_message)

    def test_upload_response_hides_ai_values_and_persists_document_id(self):
        response = self._upload()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json),
                         {"ok", "document_id", "pages", "stored_kb", "message"})
        body = json.dumps(response.json).lower()
        self.assertNotIn("comparison", body)
        self.assertNotIn("right", body)
        self.assertTrue(response.json["ok"])
        document_id = response.json["document_id"]
        cur = self.db.cursor()
        cur.execute("SELECT status,parsed_json,comparison,stored_bytes "
                    "FROM order_rx_documents WHERE document_id=%s",
                    (document_id,))
        row = cur.fetchone()
        self.assertEqual(row["status"], "PARSED")
        self.assertIsNotNone(row["parsed_json"])
        self.assertEqual(row["comparison"], "MATCH")
        self.assertLess(row["stored_bytes"], 2_000_000)
        self.assertIn(str(document_id), str(STATE["persisted"]))
        self.assertIn("PARSED", self._audit_actions(document_id))
        self.assertEqual(STATE["model_calls"][-1]["endpoint"],
                         "/checkout/rx-upload")

    def test_model_error_and_unreadable_results_are_retained(self):
        error = self.service.ai_client.ModelError("offline")

        def fail(*args):
            raise error

        STATE["read"] = fail
        response = self._upload()
        self.assertEqual(response.status_code, 200)
        document_id = response.json["document_id"]
        cur = self.db.cursor()
        cur.execute("SELECT status,parsed_json,comparison FROM "
                    "order_rx_documents WHERE document_id=%s", (document_id,))
        row = cur.fetchone()
        self.assertEqual(row["status"], "UNREADABLE")
        self.assertIsNone(row["parsed_json"])
        self.assertEqual(row["comparison"], "NO_READING")
        self.assertIn("PARSE_FAILED", self._audit_actions(document_id))
        self.assertTrue(os.path.isfile(self._file_path(document_id)))

        STATE["read"] = lambda pages, workload, endpoint: (
            None, None, "test-vision", True)
        response = self._upload()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["message"],
                         "We've kept your photo for our team, but couldn't "
                         "read it clearly. A sharper, well-lit photo of the "
                         "whole prescription helps — you can add another.")

    def test_line_and_daily_upload_limits(self):
        ids = [self._document() for _ in range(3)]
        line = [dict(self.cart[0], rx_document_ids=ids)]
        response = self._upload(cart=line)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["code"], "RX_UPLOAD_LINE_LIMIT")
        self.assertEqual(response.json["message"],
                         "You can add up to 3 photos for this frame. "
                         "Remove one to add another.")

        for _ in range(20):
            self._document()
        self._set_session(cart=self.cart)
        response = self._upload()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json["code"], "RX_UPLOAD_RATE_LIMIT")
        self.assertEqual(response.json["message"],
                         "Too many uploads today. Please try again tomorrow "
                         "or send the photo in the chat.")

    def test_storage_failure_has_customer_message(self):
        with mock.patch.object(self.service, "_write_pages",
                               side_effect=OSError("disk full")):
            response = self._upload()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json["code"], "RX_UPLOAD_STORAGE_FAILED")
        self.assertEqual(response.json["message"],
                         "We couldn't save that photo. Please try again.")

    def test_remove_requires_ownership_and_unattached_document(self):
        response = self._upload()
        document_id = response.json["document_id"]
        path = self._file_path(document_id)
        self._set_session(customer=False, email=None)
        unauth = self.client.post(
            "/checkout/rx-upload/%d/remove" % document_id)
        self.assertEqual(unauth.status_code, 401)
        self.assertEqual(unauth.json["message"],
                         "Please sign in again to add a photo.")
        self._set_session(customer=True)
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.cid + 1
        wrong_owner = self.client.post(
            "/checkout/rx-upload/%d/remove" % document_id)
        self.assertEqual(wrong_owner.status_code, 404)
        self.assertEqual(
            wrong_owner.json["message"],
            "That photo can't be removed. It may already be attached or "
            "unavailable.")
        self._set_session()
        self.assertEqual(self.client.post(
            "/checkout/rx-upload/%d/remove" % document_id).status_code, 200)
        self.assertFalse(os.path.exists(path))
        self.assertIn("REMOVED", self._audit_actions(document_id))

        response = self._upload()
        attached_id = response.json["document_id"]
        self._attach(attached_id)
        attached_remove = self.client.post(
            "/checkout/rx-upload/%d/remove" % attached_id)
        self.assertEqual(attached_remove.status_code, 404)
        self.assertEqual(
            attached_remove.json["message"],
            "That photo can't be removed. It may already be attached or "
            "unavailable.")

    def test_ensure_schema_runs_once_per_process(self):
        prior = self.service._SCHEMA_READY
        self.service._SCHEMA_READY = False
        try:
            cursor = mock.Mock()
            self.service.ensure_schema(cursor)
            self.assertEqual(cursor.execute.call_count,
                             len(self.service.TABLES) + sum(
                                 len(columns) for _table, columns
                                 in self.service.ADDED_COLUMNS))
            cursor.execute.reset_mock()
            self.service.ensure_schema(cursor)
            cursor.execute.assert_not_called()
        finally:
            self.service._SCHEMA_READY = prior

    def test_comparison_exact_transposed_axis_wrap_and_missing_eye(self):
        typed = self.service.typed_snapshot(self.cart[0])
        exact, differences = self.service.compare_prescription(
            self._proposal(), typed)
        self.assertEqual((exact, differences), ("MATCH", []))
        transposed = {
            "right": {"sph": "-1.50", "cyl": "0.50", "axis": "90",
                      "add_power": "1.50"},
            "left": self._proposal()["left"],
        }
        self.assertEqual(
            self.service.compare_prescription(transposed, typed)[0],
            "MATCH_TRANSPOSED")
        changed = json.loads(json.dumps(self._proposal()))
        changed["right"]["cyl"] = "0.50"
        changed["right"]["axis"] = "90"
        status, differences = self.service.compare_prescription(changed, typed)
        self.assertEqual(status, "DIFFERENT")
        self.assertTrue(any("R CYL: photo +0.50, typed -0.50" in item
                            for item in differences), differences)
        axis_values = dict(typed, right_cyl="0", right_axis="0")
        axis_photo = {
            "right": {"sph": "-1.00", "cyl": "0", "axis": "180",
                      "add": "1.50"},
            "left": self._proposal()["left"],
        }
        self.assertEqual(self.service.compare_prescription(
            axis_photo, axis_values)[0], "MATCH")
        missing = {"right": self._proposal()["right"]}
        self.assertTrue(any("L: not readable on photo" in item for item in
                            self.service.compare_prescription(
                                missing, typed)[1]))
        self.assertEqual(self.service.compare_prescription(None, typed),
                         ("NO_READING", []))

    def test_comparison_requires_typed_values_for_readable_photo_eyes(self):
        status, differences = self.service.compare_prescription(
            self._proposal(), {})
        self.assertEqual(status, "DIFFERENT")
        self.assertIn("R: no typed value on the order", differences)
        self.assertIn("L: no typed value on the order", differences)

    def test_checkout_attachment_ownership_retention_and_idempotent_reuse(self):
        document_id = self._upload().json["document_id"]
        with self.client.session_transaction() as sess:
            cart = sess["cart"]
        self._attach(document_id, cart=cart)
        self._attach(document_id, cart=cart)
        cur = self.db.cursor()
        cur.execute("SELECT order_id,order_rx_id,retain_until,comparison "
                    "FROM order_rx_documents WHERE document_id=%s",
                    (document_id,))
        row = cur.fetchone()
        self.assertEqual(row["order_id"], self.order_id)
        self.assertEqual(row["order_rx_id"], self.cid)
        self.assertEqual(row["comparison"], "MATCH")
        expected = self.lens_rx.retain_until(datetime.datetime.now())
        if isinstance(expected, datetime.datetime):
            expected = expected.date()
        self.assertEqual(row["retain_until"], expected)
        self.assertEqual(self._audit_actions(document_id).count("ATTACHED"), 1)

        other = self._document(customer_id=self.cid + 10)
        self.service.attach_for_order(
            self.db.cursor(), self.order_id, self.cid, [
                dict(self.cart[0], rx_document_ids=[other])])
        self.db.commit()
        cur.execute("SELECT order_id FROM order_rx_documents "
                    "WHERE document_id=%s", (other,))
        self.assertIsNone(cur.fetchone()["order_id"])

    def test_chat_attachment_is_bounded_audited_and_idempotent(self):
        line = dict(self.cart[0], order_id=self.order_id,
                    order_line_id=self.line_id, product_id=self.pid,
                    rx_id=self.cid,
                    typed=self.service.typed_snapshot(self.cart[0]))
        source = _image_bytes("JPEG", (4000, 3000), noise=True)
        with self.app.test_request_context("/", base_url="https://optiwar.com"):
            cursor = self.db.cursor()
            document_id = self.service.attach_chat_photo(
                cursor, self.cid, 87654321, line, source,
                self._proposal(), 0.96, "test-vision")
            duplicate = self.service.attach_chat_photo(
                cursor, self.cid, 87654321, line, source,
                self._proposal(), 0.96, "test-vision")
        self.assertEqual(document_id, duplicate)
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT source,chat_attachment_id,order_id,order_rx_id,"
            "comparison,stored_bytes FROM order_rx_documents "
            "WHERE document_id=%s", (document_id,))
        row = cursor.fetchone()
        self.assertEqual(row["source"], "chat")
        self.assertEqual(row["chat_attachment_id"], 87654321)
        self.assertEqual(row["order_id"], self.order_id)
        self.assertEqual(row["order_rx_id"], self.cid)
        self.assertEqual(row["comparison"], "MATCH")
        self.assertLess(row["stored_bytes"], 2_000_000)
        self.assertEqual(self._audit_actions(document_id),
                         ["UPLOADED", "ATTACHED", "PARSED"])
        self.assertEqual(os.stat(self._file_path(document_id)).st_mode & 0o777,
                         0o600)

    def test_preparing_rx_lines_uses_paid_status_and_existing_eye_parsing(self):
        class Cursor:
            def execute(self, sql, params):
                self.sql = sql
                self.params = params

            def fetchall(self):
                return [
                    {"order_id": "OW-22", "order_line_id": 3, "product_id": 4,
                     "rx_id": 5, "product_name": "Newest frame",
                     "order_status_name": "Processed",
                     "right_eye": "-1.00/-0.50/180/1.50",
                     "left_eye": "-2.00/-0.25/90/1.50"},
                    {"order_id": "OW-21", "order_line_id": 2, "product_id": 4,
                     "rx_id": 6, "product_name": "Shipped frame",
                     "order_status_name": "Shipped",
                     "right_eye": "-1.00///", "left_eye": None},
                    {"order_id": "OW-20", "order_line_id": 1, "product_id": 4,
                     "rx_id": 7, "product_name": "No Rx frame",
                     "order_status_name": "Processed",
                     "right_eye": None, "left_eye": None},
                ]

        cursor = Cursor()
        lines = self.service.preparing_rx_lines(cursor, self.cid)
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            lines[0],
            {"order_id": "OW-22", "order_line_id": 3, "product_id": 4,
             "rx_id": 5, "product_name": "Newest frame",
             "typed": {"right_pwr": -1.0, "right_cyl": -0.5,
                       "right_axis": 180.0, "right_add": 1.5,
                       "left_pwr": -2.0, "left_cyl": -0.25,
                       "left_axis": 90.0, "left_add": 1.5}})
        self.assertIn("pc.status='TXN_SUCCESS'", cursor.sql)
        self.assertIn("o.is_test=0 AND o.archived=0", cursor.sql)
        self.assertEqual(cursor.params, (self.cid,))

    def test_checkout_documents_only_returns_owned_live_files(self):
        document_id = self._upload().json["document_id"]
        with self.client.session_transaction() as sess:
            cart = sess["cart"]
        docs = self.service.checkout_documents(self.db.cursor(), self.cid, cart)
        self.assertEqual([d["document_id"] for d in
                          docs[0]], [document_id])
        self._attach(document_id)
        self.assertEqual(self.service.checkout_documents(
            self.db.cursor(), self.cid, cart)[0], [])

    def test_ops_auth_cache_order_lookup_signed_files_and_audit(self):
        document_id = self._upload().json["document_id"]
        self._attach(document_id)
        line = dict(self.cart[0], order_id=self.order_id,
                    order_line_id=self.line_id, product_id=self.pid,
                    rx_id=self.cid)
        with self.app.test_request_context("/", base_url="https://optiwar.com"):
            self.service.attach_chat_photo(
                self.db.cursor(), self.cid, 87654322, line,
                _image_bytes("JPEG"), self._proposal(), 0.96, "test-vision")
        response = self.client.get(
            "/ops/api/orders/ow-%s/rx-documents" % self.order_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Pragma"], "no-cache")
        self.assertEqual(response.json["order_id"], self.order_id)
        self.assertIn("AI suggestion — check against the photo, do not copy blindly",
                      self.client.get("/ops/rx-documents?order=" +
                                      self.order_id).get_data(as_text=True))
        page = self.client.get("/ops/rx-documents?order=" + self.order_id)
        self.assertEqual(page.status_code, 200)
        self.assertIn("From chat", page.get_data(as_text=True))
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertEqual(page.headers["Pragma"], "no-cache")
        self.assertEqual(self.client.get(
            "/ops/api/orders/UNKNOWN/rx-documents").status_code, 404)
        not_found = self.client.get(
            "/ops/rx-documents?order=UNKNOWN")
        self.assertEqual(not_found.status_code, 404)
        self.assertEqual(not_found.headers["Cache-Control"], "no-store")
        self.assertEqual(not_found.headers["Pragma"], "no-cache")
        api_not_found = self.client.get(
            "/ops/api/orders/UNKNOWN/rx-documents")
        self.assertEqual(api_not_found.status_code, 404)
        self.assertEqual(api_not_found.headers["Cache-Control"], "no-store")
        self.assertEqual(api_not_found.headers["Pragma"], "no-cache")

        files = response.json["lines"][0]["documents"][0]["files"]
        view = files[0]["view_url"]
        missing = self.client.get(view.split("?")[0])
        self.assertEqual(missing.status_code, 403)
        from urllib.parse import parse_qs, urlsplit
        split = urlsplit(view)
        query = parse_qs(split.query)
        expired = self.client.get(
            split.path + "?exp=1&sig=" + query["sig"][0])
        self.assertEqual(expired.status_code, 403)
        forged = self.client.get(
            split.path + "?exp=" + query["exp"][0] + "&sig=" + "0" * 64)
        self.assertEqual(forged.status_code, 403)
        viewed = self.client.get(view)
        self.assertEqual(viewed.status_code, 200)
        self.assertEqual(viewed.mimetype, "image/jpeg")
        self.assertEqual(viewed.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(viewed.headers["Cache-Control"], "no-store")
        downloaded = self.client.get(files[0]["download_url"])
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("attachment;", downloaded.headers["Content-Disposition"])
        self.assertEqual(downloaded.headers["Cache-Control"], "no-store")
        actions = self._audit_actions(document_id)
        self.assertIn("LINK_ISSUED", actions)
        self.assertIn("VIEWED", actions)
        self.assertIn("DOWNLOADED", actions)

        STATE["authorized"] = True
        bearer = self.client.get(
            "/ops/api/orders/%s/rx-documents" % self.order_id,
            headers={"Authorization": "Bearer test-token"})
        self.assertEqual(bearer.status_code, 200)
        STATE["authorized"] = False
        for path in ("/ops/rx-documents",
                     "/ops/api/orders/%s/rx-documents" % self.order_id,
                     view,
                     "/ops/api/rx-documents/%d/review" % document_id):
            if path.endswith("/review"):
                unauth = self.client.post(path, json={
                    "verdict": "MATCHES_ORDER"})
            else:
                unauth = self.client.get(path)
            self.assertEqual(unauth.status_code, 401)
            self.assertEqual(unauth.headers["Cache-Control"], "no-store")
            self.assertEqual(unauth.headers["Pragma"], "no-cache")

    def test_review_note_validation_and_reparse_do_not_change_order_rx(self):
        document_id = self._upload().json["document_id"]
        self._attach(document_id)
        before = self.db.cursor()
        before.execute("SELECT * FROM rx_collector WHERE rx_id=%s",
                       (self.cid,))
        original = before.fetchone()
        bad = self.client.post(
            "/ops/api/rx-documents/%d/review" % document_id,
            json={"verdict": "DIFFERENT_CONTACT_CUSTOMER", "note": ""})
        self.assertEqual(bad.status_code, 400)
        reviewed = self.client.post(
            "/ops/api/rx-documents/%d/review" % document_id,
            json={"verdict": "DIFFERENT_CONTACT_CUSTOMER",
                  "note": "Please confirm the axis."})
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.json["verdict"], "DIFFERENT_CONTACT_CUSTOMER")
        STATE["read"] = lambda pages, workload, endpoint: (
            self._proposal(), 0.92, "second-model", False)
        parsed = self.client.post(
            "/ops/api/rx-documents/%d/reparse" % document_id)
        self.assertEqual(parsed.status_code, 200)
        self.assertEqual(parsed.json["comparison"], "MATCH")
        self.assertIn("REVIEWED", self._audit_actions(document_id))
        self.assertIn("REPARSED", self._audit_actions(document_id))
        before.execute("SELECT * FROM rx_collector WHERE rx_id=%s",
                       (self.cid,))
        self.assertEqual(before.fetchone(), original)

    def test_purge_removes_expired_unattached_but_keeps_attached_retention(self):
        old = datetime.datetime.now() - datetime.timedelta(days=31)
        document_id = self._document(created_at=old)
        path = self._write_file(document_id)
        with self.app.app_context():
            self.service.purge_expired(
                self.db, today=datetime.date.today(),
                now=datetime.datetime.now())
        self.assertFalse(os.path.exists(path))
        cur = self.db.cursor()
        cur.execute("SELECT status,purged_at FROM order_rx_documents "
                    "WHERE document_id=%s", (document_id,))
        row = cur.fetchone()
        self.assertEqual(row["status"], "PURGED")
        self.assertIsNotNone(row["purged_at"])
        self.assertIn("PURGED", self._audit_actions(document_id))

        attached = self._document()
        self.service.attach_for_order(
            self.db.cursor(), self.order_id, self.cid, [
                dict(self.cart[0], rx_document_ids=[attached])])
        self.db.commit()
        cur.execute("SELECT retain_until FROM order_rx_documents "
                    "WHERE document_id=%s", (attached,))
        future = cur.fetchone()["retain_until"] - datetime.timedelta(days=1)
        with self.app.app_context():
            self.service.purge_expired(self.db, today=future)
        cur.execute("SELECT status FROM order_rx_documents "
                    "WHERE document_id=%s", (attached,))
        self.assertNotEqual(cur.fetchone()["status"], "PURGED")

    def test_attachment_failure_is_nonfatal_and_recorded(self):
        class BadCursor:
            def execute(self, *args):
                raise RuntimeError("test database failure")

        with self.app.test_request_context("/checkout"):
            result = self.service.attach_for_order(
                BadCursor(), self.order_id, self.cid, self.cart)
        self.assertFalse(result)
        self.assertTrue(STATE["defects"])


class RxUploadKnowledgeBaseTests(unittest.TestCase):
    def test_customer_answers_are_exact_and_not_denials(self):
        with open(os.path.join(REPO, "static", "ai",
                               "optiwar_ai_knowledge_base.json"),
                  encoding="utf-8") as fh:
            faq = json.load(fh)["faq"]
        answers = {item["intent"]: item["answer"] for item in faq
                   if item.get("intent") in
                   ("rx_upload", "rx_upload_after_order")}
        self.assertEqual(
            answers["rx_upload"],
            "Yes. Enter your values on the prescription step, then at checkout "
            "tap 'Add a photo or screenshot' under the frame — a phone photo "
            "or a screenshot works. Our team checks your typed values against "
            "the photo before your lenses are made.")
        self.assertEqual(
            answers["rx_upload_after_order"],
            "Attach the photo here in the chat with the photo button; our team "
            "will check it against your order before your lenses are made.")
        other_answers = [
            item["answer"] for item in faq
            if item.get("intent") not in
            ("rx_upload", "rx_upload_after_order")
            and (
                "prescription image upload" in item.get("answer", "").lower()
                or "upload a prescription image" in
                item.get("answer", "").lower())
        ]
        self.assertGreaterEqual(len(other_answers), 2)
        self.assertFalse(any(
            "cannot" in answer.lower() or "can't" in answer.lower()
            for answer in other_answers))
        self.assertFalse(any("cannot send" in answer.lower()
                            for answer in answers.values()))


if __name__ == "__main__":
    unittest.main()
