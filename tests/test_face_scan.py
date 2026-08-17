"""The face-scan journey, against a real database where one is available.

The questions worth asking of this feature are not "does it store a file". They
are: can a link be used twice, can a photo be fetched without authority, can a
measurement be recorded that the fitting logic would refuse, and does the photo
actually disappear when retention says so. Each has a test here.

    OPTIWAR_TEST_MYSQL_DB=optiwar2 python3 -m unittest tests.test_face_scan
"""
import hashlib
import importlib.util
import io
import os
import sys
import types
import unittest
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 4096
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 4096

DB_CONF = dict(
    host=os.environ.get("OPTIWAR_TEST_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("OPTIWAR_TEST_MYSQL_PORT", "3306")),
    user=os.environ.get("OPTIWAR_TEST_MYSQL_USER", "oslb6"),
    password=os.environ.get("OPTIWAR_TEST_MYSQL_PASSWORD", "testpw"),
    database=os.environ.get("OPTIWAR_TEST_MYSQL_DB", "optiwar2"),
)


def _connect():
    import pymysql
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor,
                           autocommit=False, connect_timeout=5, **DB_CONF)


def _available():
    try:
        _connect().close()
        return True
    except Exception:  # noqa: BLE001
        return False


AVAILABLE = _available()


def _load_face_scan(app_root, get_db, ops_auth):
    """Import face_scan.py with its two package imports satisfied.

    The module lives in a Flask package whose __init__ builds the whole
    application; loading that to test an upload token would test everything
    except the upload token. So ``.db`` and ``.ops`` are supplied as stubs and
    the module itself is the real one.
    """
    pkg_name = "fs_pkg"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [REPO]
    sys.modules[pkg_name] = pkg

    db_mod = types.ModuleType(pkg_name + ".db")
    db_mod.get_db = get_db
    sys.modules[pkg_name + ".db"] = db_mod

    ops_mod = types.ModuleType(pkg_name + ".ops")
    ops_mod._require_ops_auth = ops_auth
    sys.modules[pkg_name + ".ops"] = ops_mod

    notif = types.ModuleType(pkg_name + ".notifications")
    notif.sent = []

    def send_whatsapp_tracked(to, template, components=None):
        notif.sent.append((to, template, components))
        return {"ok": True, "request_id": "rid_%d" % len(notif.sent),
                "status": "sent", "error": ""}

    notif.send_whatsapp_tracked = send_whatsapp_tracked
    sys.modules[pkg_name + ".notifications"] = notif

    spec = importlib.util.spec_from_file_location(
        pkg_name + ".face_scan", os.path.join(REPO, "face_scan.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod._test_notifications = notif
    return mod


@unittest.skipUnless(AVAILABLE, "no MariaDB test database (see scripts/setup_test_db.sh)")
class FaceScanJourneyTests(unittest.TestCase):
    """Request -> link -> photo -> measurement, and the ways it must refuse."""

    @classmethod
    def setUpClass(cls):
        from flask import Flask
        cls.authorised = [True]
        cls.db = _connect()

        def get_db():
            return cls.db

        cls.fs = _load_face_scan(REPO, get_db, lambda: cls.authorised[0])
        cls.app = Flask(__name__, root_path=REPO,
                        template_folder=os.path.join(REPO, "templates"))
        cls.app.config.update(TESTING=True, SECRET_KEY="test",
                              FACE_SCAN_TOKEN_HOURS=72,
                              FACE_SCAN_RETENTION_DAYS=90,
                              FACE_SCAN_WA_TEMPLATE="",
                              FACE_SCAN_LINK_BASE="https://optiwar.in")
        cls.app.register_blueprint(cls.fs.bp)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        self.authorised[0] = True
        self.created = []
        self.ctx = self.app.test_request_context()
        self.ctx.push()
        self.fs.ensure_schema(self.db)

    def tearDown(self):
        cur = self.db.cursor()
        for rid in self.created:
            row = self.fs._by_id(self.db, rid)
            if row and row.get("image_path"):
                try:
                    os.remove(os.path.join(self.fs._upload_dir(),
                                           row["image_path"]))
                except OSError:
                    pass
            cur.execute("DELETE FROM face_scan_audit WHERE request_id=%s", (rid,))
            cur.execute("DELETE FROM face_scan_requests WHERE request_id=%s", (rid,))
        self.db.commit()
        cur.close()
        self.ctx.pop()

    # ── helpers ──

    def _request(self, **kw):
        rid, token = self.fs.create_request(
            self.db, contact_name="Test Customer", contact_phone="919812345678",
            created_by="admin@optiwar.com", **kw)
        self.created.append(rid)
        return rid, token

    def _upload(self, token, data=JPEG, name="face.jpg", consent="1"):
        return self.client.post(
            "/face-scan/%s" % token,
            data={"photo": (io.BytesIO(data), name), "consent": consent},
            content_type="multipart/form-data")

    # ── the journey ──

    def test_the_short_journey_end_to_end(self):
        rid, token = self._request()
        self.assertEqual(self.client.get("/face-scan/%s" % token).status_code, 200)
        resp = self._upload(token)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        row = self.fs._by_id(self.db, rid)
        self.assertEqual(row["status"], self.fs.ST_UPLOADED)
        self.assertEqual(row["image_sha256"], hashlib.sha256(JPEG).hexdigest())
        self.assertIsNotNone(row["consent_at"])
        self.assertIsNotNone(row["purge_after"])

        ok, error = self.fs.record_measurements(self.db, rid, 63.5, 138.0,
                                                actor="staff@optiwar.com")
        self.assertTrue(ok, error)
        row = self.fs._by_id(self.db, rid)
        self.assertEqual(float(row["pd_mm"]), 63.5)
        self.assertEqual(float(row["face_width_mm"]), 138.0)
        self.assertEqual(row["measured_by"], "staff@optiwar.com")
        self.assertEqual(row["status"], self.fs.ST_MEASURED)

    def test_the_link_is_single_use(self):
        _, token = self._request()
        self.assertEqual(self._upload(token).status_code, 200)
        second = self._upload(token, data=PNG, name="again.png")
        self.assertEqual(second.status_code, 410)
        self.assertEqual(second.get_json()["error"], "used")
        # and the page says so rather than offering a form that would be refused
        page = self.client.get("/face-scan/%s" % token)
        self.assertEqual(page.status_code, 410)
        self.assertIn(b"no longer usable", page.data)

    def test_an_expired_link_is_refused(self):
        rid, token = self._request()
        cur = self.db.cursor()
        cur.execute("UPDATE face_scan_requests SET expires_at=%s WHERE request_id=%s",
                    (datetime.now() - timedelta(minutes=1), rid))
        self.db.commit()
        cur.close()
        self.assertEqual(self._upload(token).get_json()["error"], "expired")

    def test_an_unknown_token_is_not_a_server_error(self):
        resp = self._upload("not-a-real-token")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.client.get("/face-scan/nope").status_code, 410)

    def test_the_token_is_not_stored_in_recoverable_form(self):
        rid, token = self._request()
        row = self.fs._by_id(self.db, rid)
        self.assertNotIn(token, str(row.values()))
        self.assertEqual(row["token_hash"],
                         hashlib.sha256(token.encode()).hexdigest())

    # ── consent and validation ──

    def test_a_photo_without_consent_is_not_stored(self):
        rid, token = self._request()
        resp = self._upload(token, consent="0")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "consent_required")
        self.assertIsNone(self.fs._by_id(self.db, rid)["image_path"])

    def test_a_file_whose_content_is_not_its_extension_is_refused(self):
        rid, token = self._request()
        resp = self._upload(token, data=b"MZ" + b"\x00" * 4096, name="face.jpg")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not the image type", resp.get_json()["error"])
        row = self.fs._by_id(self.db, rid)
        self.assertIsNone(row["image_path"])
        # rejected, but counted: the validator is not a free oracle
        self.assertEqual(row["upload_attempts"], 1)
        self.assertEqual(row["status"], self.fs.ST_REQUESTED)

    def test_a_pdf_is_not_a_face_photo(self):
        _, token = self._request()
        resp = self._upload(token, data=b"%PDF-1.4" + b"\x00" * 4096,
                            name="scan.pdf")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("photo", resp.get_json()["error"])

    def test_repeated_rejections_eventually_close_the_link(self):
        rid, token = self._request()
        for _ in range(self.fs.MAX_UPLOAD_ATTEMPTS):
            self._upload(token, data=b"MZ" + b"\x00" * 4096, name="x.jpg")
        self.assertEqual(self._upload(token).get_json()["error"],
                         "too_many_attempts")

    # ── measurements ──

    def test_a_measurement_the_fitting_logic_would_refuse_is_refused_here(self):
        rid, token = self._request()
        self._upload(token)
        for pd, width in ((63.0, 42.0), (63.0, 400.0), (5.0, 138.0),
                          ("wide", 138.0)):
            ok, error = self.fs.record_measurements(self.db, rid, pd, width,
                                                    actor="staff@optiwar.com")
            self.assertFalse(ok, "accepted pd=%s width=%s" % (pd, width))
            self.assertTrue(error)
        self.assertIsNone(self.fs._by_id(self.db, rid)["pd_mm"])

    def test_no_measurement_without_a_photo_to_read_it_from(self):
        rid, _ = self._request()
        ok, error = self.fs.record_measurements(self.db, rid, 63.0, 138.0,
                                                actor="staff@optiwar.com")
        self.assertFalse(ok)
        self.assertIn("No photo", error)

    def test_the_scale_reference_must_be_confirmed(self):
        rid, token = self._request()
        self._upload(token)
        resp = self.client.post("/admin/face-scan/%s/measurements" % rid,
                                data={"pd_mm": "63", "face_width_mm": "138"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"],
                         "scale_reference_not_confirmed")
        resp = self.client.post("/admin/face-scan/%s/measurements" % rid,
                                data={"pd_mm": "63", "face_width_mm": "138",
                                      "scale_confirmed": "1"})
        self.assertTrue(resp.get_json()["success"])

    # ── authority ──

    def test_the_photo_is_not_readable_without_admin_authority(self):
        rid, token = self._request()
        self._upload(token)
        self.assertEqual(
            self.client.get("/admin/face-scan/%s/image" % rid).status_code, 200)
        self.authorised[0] = False
        for path in ("", "/%s" % rid, "/%s/image" % rid):
            resp = self.client.get("/admin/face-scan%s" % path)
            self.assertEqual(resp.status_code, 401, path)
        self.assertEqual(
            self.client.post("/admin/face-scan/new",
                             data={"phone": "919812345678"}).status_code, 401)
        self.assertEqual(
            self.client.post("/admin/face-scan/%s/measurements" % rid,
                             data={"pd_mm": "63", "face_width_mm": "138",
                                   "scale_confirmed": "1"}).status_code, 401)

    def test_the_admin_json_never_returns_the_token_hash(self):
        rid, _ = self._request()
        body = self.client.get("/admin/face-scan/%s?format=json" % rid).get_json()
        self.assertNotIn("token_hash", body)
        self.assertIn("status", body)

    # ── sending ──

    def test_without_an_approved_template_nothing_claims_to_have_been_sent(self):
        rid, token = self._request()
        row = self.fs._by_id(self.db, rid)
        result = self.fs.send_request_whatsapp(self.db, row, token)
        self.assertEqual(result["error"], "no_template")
        self.assertEqual(self.fs._by_id(self.db, rid)["status"],
                         self.fs.ST_SEND_FAILED)
        # and the link still works: staff can deliver it another way
        self.assertEqual(self._upload(token).status_code, 200)

    def test_the_link_carries_the_token_and_an_absolute_host(self):
        rid, token = self._request()
        row = self.fs._by_id(self.db, rid)
        self.app.config["FACE_SCAN_WA_TEMPLATE"] = "face_scan_request"
        try:
            result = self.fs.send_request_whatsapp(self.db, row, token)
        finally:
            self.app.config["FACE_SCAN_WA_TEMPLATE"] = ""
        self.assertTrue(result["ok"])
        to, template, components = self.fs._test_notifications.sent[-1]
        self.assertEqual(to, "919812345678")
        self.assertEqual(template, "face_scan_request")
        link = components["body_2"]["value"]
        self.assertTrue(link.startswith("https://optiwar.in/face-scan/"), link)
        self.assertIn(token, link)
        self.assertEqual(self.fs._by_id(self.db, rid)["status"], self.fs.ST_SENT)

    # ── retention ──

    def test_retention_deletes_the_photo_and_keeps_the_measurement(self):
        rid, token = self._request()
        self._upload(token)
        self.fs.record_measurements(self.db, rid, 63.0, 138.0,
                                    actor="staff@optiwar.com")
        row = self.fs._by_id(self.db, rid)
        path = os.path.join(self.fs._upload_dir(), row["image_path"])
        self.assertTrue(os.path.exists(path))

        cur = self.db.cursor()
        cur.execute("UPDATE face_scan_requests SET purge_after=%s WHERE request_id=%s",
                    (datetime.now() - timedelta(days=1), rid))
        self.db.commit()
        cur.close()
        self.assertIn(rid, self.fs.purge_due_images(self.db))
        self.assertFalse(os.path.exists(path))
        row = self.fs._by_id(self.db, rid)
        self.assertIsNone(row["image_path"])
        self.assertIsNotNone(row["purged_at"])
        self.assertEqual(float(row["pd_mm"]), 63.0)
        # and the photo cannot be fetched after it is gone
        self.assertEqual(
            self.client.get("/admin/face-scan/%s/image" % rid).status_code, 404)

    def test_a_second_purge_is_a_no_op(self):
        rid, token = self._request()
        self._upload(token)
        cur = self.db.cursor()
        cur.execute("UPDATE face_scan_requests SET purge_after=%s WHERE request_id=%s",
                    (datetime.now() - timedelta(days=1), rid))
        self.db.commit()
        cur.close()
        self.fs.purge_due_images(self.db)
        self.assertNotIn(rid, self.fs.purge_due_images(self.db))

    # ── audit ──

    def test_every_step_leaves_an_audited_trace(self):
        rid, token = self._request()
        self.client.get("/face-scan/%s" % token)
        self._upload(token)
        self.client.get("/admin/face-scan/%s/image" % rid)
        self.fs.record_measurements(self.db, rid, 63.0, 138.0,
                                    actor="staff@optiwar.com")
        cur = self.db.cursor()
        cur.execute("""SELECT action, actor FROM face_scan_audit
                        WHERE request_id=%s ORDER BY id""", (rid,))
        actions = [r["action"] for r in cur.fetchall()]
        cur.close()
        self.assertEqual(actions, ["CREATED", "OPENED", "UPLOADED",
                                   "IMAGE_VIEWED", "MEASURED"])


if __name__ == "__main__":
    unittest.main()
