"""PR-C: an uploaded prescription is normalised, read by the configured vision
provider, judged by the lens's own validator, and proposed to the customer —
who confirms it through Add to Cart. Ops reach the stored pages only through
a signed, expiring link, and every step leaves an audit row.

Unit tests need no database. ``OnMariaDB`` cases prove the routes against the
CI MariaDB and are skipped without one.
"""
import datetime
import importlib.util
import ast
import io
import os
import shutil
import sys
import tempfile
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

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


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


lens_order = _load("lens_order")
lens_rx = _load("lens_rx")
lens_documents = _load("lens_documents")


def _jpeg(w=1200, h=900, orient=None):
    from PIL import Image
    image = Image.new("RGB", (w, h), (250, 250, 250))
    out = io.BytesIO()
    exif = None
    if orient:
        exif = Image.Exif()
        exif[0x0112] = orient
    image.save(out, format="JPEG", quality=92,
               **({"exif": exif.tobytes()} if exif else {}))
    return out.getvalue()


def _png(w=800, h=600):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGBA", (w, h), (200, 200, 200, 255)).save(out, format="PNG")
    return out.getvalue()


def _pdf(pages=1):
    import pymupdf
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 72), "OD -3.75  OS -2.50  page %d" % (i + 1))
    return doc.tobytes()


class Normalisation(unittest.TestCase):

    def test_the_kind_is_read_from_the_bytes_not_the_name(self):
        self.assertEqual(lens_documents.kind_of(_jpeg()), "jpeg")
        self.assertEqual(lens_documents.kind_of(_png()), "png")
        self.assertEqual(lens_documents.kind_of(_pdf()), "pdf")
        self.assertEqual(lens_documents.kind_of(
            b"RIFF\x00\x00\x00\x00WEBPVP8 "), "webp")
        self.assertEqual(lens_documents.kind_of(
            b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8), "heic")
        self.assertIsNone(lens_documents.kind_of(b"<html>" + b"\x00" * 20))
        self.assertIsNone(lens_documents.kind_of(b"GIF89a" + b"\x00" * 20))

    def test_size_bounds_and_foreign_types_are_refused_before_anything_else(self):
        for data in (b"", b"\xff\xd8\xff" + b"\x00" * 10,
                     b"\xff\xd8\xff" + b"\x00" * (lens_documents.MAX_UPLOAD_BYTES),
                     b"MZ" + b"\x00" * 5000, b"<svg>" + b"\x00" * 5000):
            with self.assertRaises(lens_documents.Rejected) as cm:
                lens_documents.normalise(data)
            self.assertEqual(cm.exception.code, "RX_UPLOAD_REJECTED")

    def test_a_jpeg_with_a_jpeg_header_but_no_image_is_refused(self):
        with self.assertRaises(lens_documents.Rejected):
            lens_documents.normalise(b"\xff\xd8\xff\xe0" + b"\x00" * 4096)

    def test_an_image_becomes_one_bounded_jpeg_page_with_orientation_applied(self):
        from PIL import Image
        norm = lens_documents.normalise(_jpeg(4000, 3000, orient=6))
        self.assertEqual(norm["kind"], "jpeg")
        self.assertEqual(len(norm["pages"]), 1)
        self.assertEqual(len(norm["sha256"]), 64)
        page = Image.open(io.BytesIO(norm["pages"][0]))
        self.assertEqual(page.format, "JPEG")
        # orientation 6 rotates to portrait; longest side bounded to 2000
        self.assertLess(page.width, page.height)
        self.assertLessEqual(max(page.size), lens_documents.MAX_SIDE_PX)
        self.assertNotIn("exif", page.info)

    def test_a_png_with_alpha_is_flattened_to_jpeg(self):
        from PIL import Image
        norm = lens_documents.normalise(_png())
        self.assertEqual(norm["kind"], "png")
        self.assertEqual(Image.open(io.BytesIO(norm["pages"][0])).mode, "RGB")

    def test_a_pdf_is_rasterised_page_by_page_and_bounded(self):
        norm = lens_documents.normalise(_pdf(pages=4))
        self.assertEqual(norm["kind"], "pdf")
        self.assertEqual(len(norm["pages"]), lens_documents.MAX_PDF_PAGES)
        for page in norm["pages"]:
            self.assertEqual(lens_documents.kind_of(page), "jpeg")

    def test_a_broken_pdf_is_refused(self):
        with self.assertRaises(lens_documents.Rejected):
            lens_documents.normalise(b"%PDF-1.4 " + b"garbage" * 400)

    def test_the_stored_name_carries_nothing_of_the_customer(self):
        self.assertEqual(lens_documents.stored_name(17, 1), "cldoc-17-1.jpg")
        self.assertNotIn("/", lens_documents.stored_name("17", "0"))


class Providers(unittest.TestCase):

    def test_openai_vision_is_live_and_deepseek_is_selected_by_config(self):
        self.assertEqual(lens_documents.provider_name(None), "openai_vision")
        self.assertEqual(lens_documents.provider_name(""), "openai_vision")
        self.assertEqual(lens_documents.provider_name("nonsense"), "openai_vision")
        self.assertEqual(lens_documents.provider_name("DeepSeek_Vision"),
                         "deepseek_vision")

    def test_both_workloads_are_declared_in_the_client_and_the_registry(self):
        ai_client = open(os.path.join(REPO, "ai_client.py")).read()
        registry = open(os.path.join(REPO, "ai_model_registry.py")).read()
        for name in lens_documents.PROVIDERS:
            self.assertIn('"%s": {' % name, ai_client)
            self.assertIn('"%s": {' % name, registry)
        self.assertIn('"OPENAI_VISION_MODEL", "gpt-4o"', ai_client)
        self.assertIn('"DEEPSEEK_VISION_MODEL"', ai_client)
        self.assertIn("LENS_RX_VISION_PROVIDER", registry)

    def test_the_messages_carry_the_prompt_and_every_page_inline(self):
        msgs = lens_documents.messages_for([b"\xff\xd8a", b"\xff\xd8b"])
        self.assertEqual(len(msgs), 1)
        content = msgs[0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("do not guess", content[0]["text"])
        self.assertEqual([c["type"] for c in content[1:]],
                         ["image_url", "image_url"])
        self.assertTrue(content[1]["image_url"]["url"]
                        .startswith("data:image/jpeg;base64,"))


class Reading(unittest.TestCase):

    def test_a_reading_arrives_in_the_ask_ai_proposal_shape(self):
        proposal, conf, unreadable = lens_documents.parse_reading(
            '```json\n{"right": {"sph": "-3.75", "bc": "8.6"}, '
            '"left": {"SPH": "-2.50", "cyl": "", "axis": ""}, '
            '"confidence": 0.91, "unreadable": false}\n```')
        self.assertFalse(unreadable)
        self.assertEqual(conf, 0.91)
        self.assertEqual(proposal["right"]["sph"], "-3.75")
        self.assertEqual(proposal["right"]["bc"], "8.60")
        self.assertEqual(proposal["left"]["sph"], "-2.50")
        self.assertNotIn("cyl", proposal["left"])
        # the same mapping the chat tag goes through
        self.assertEqual(proposal, lens_rx.proposal_from_mapping(
            {"right": {"sph": "-3.75", "bc": "8.6"}, "left": {"SPH": "-2.50"}}))

    def test_malformed_or_refusing_answers_are_unreadable_not_errors(self):
        for text in ("", "I cannot read this.", "{not json", "[1,2]",
                     '{"unreadable": true, "confidence": 0.2}',
                     '{"right": {"cyl": "-0.75"}, "left": {}}',
                     '{"right": {"sph": "abc"}}'):
            proposal, _, unreadable = lens_documents.parse_reading(text)
            self.assertIsNone(proposal, text)
            self.assertTrue(unreadable, text)

    def test_confidence_is_clamped_and_optional(self):
        _, conf, _ = lens_documents.parse_reading(
            '{"right": {"sph": "-1.00"}, "confidence": 7}')
        self.assertEqual(conf, 1.0)
        _, conf, _ = lens_documents.parse_reading(
            '{"right": {"sph": "-1.00"}, "confidence": "high"}')
        self.assertIsNone(conf)

    def test_a_single_eye_reading_leaves_the_other_eye_none(self):
        proposal, _, _ = lens_documents.parse_reading(
            '{"left": {"sph": "+1.25"}}')
        self.assertIsNone(proposal["right"])
        self.assertEqual(proposal["left"]["sph"], "1.25")   # canonical, unsigned plus

    def test_ask_ai_extract_proposal_still_reads_the_chat_tag(self):
        text = ('Here you go [LENS_RX:{"right": {"sph": "-3.75"}, '
                '"left": {"sph": "-2.50"}}]')
        cleaned, proposal = lens_rx.extract_proposal(text)
        self.assertEqual(cleaned, "Here you go")
        self.assertEqual(proposal["right"]["sph"], "-3.75")
        self.assertEqual(proposal["left"]["sph"], "-2.50")
        self.assertEqual(lens_rx.extract_proposal("plain"), ("plain", None))
        self.assertEqual(lens_rx.extract_proposal("[LENS_RX:{bad}]")[1], None)


class KetAndOps(unittest.TestCase):

    def test_the_ket_reference_and_ops_view_carry_no_powers(self):
        row = {"document_id": 5, "customer_id": 498, "product_id": 1015,
               "site": "optiwar.com", "status": "PARSED", "original_kind": "jpeg",
               "original_bytes": 5000, "stored_bytes": 4000, "pages": 1,
               "provider": "openai_vision", "model": "gpt-4o",
               "parsed_json": '{"eyes": {"right": {"sph": "-3.75"}}}',
               "confidence": 0.9, "refusal": None, "ket_ref": "KET-77",
               "created_at": datetime.datetime(2026, 9, 9, 10, 0),
               "parsed_at": None, "confirmed_at": None,
               "retain_until": datetime.date(2028, 9, 9), "purged_at": None}
        view = lens_documents.ops_view(row)
        self.assertNotIn("parsed_json", view)
        self.assertNotIn("-3.75", str(view))
        self.assertEqual(view["confidence"], 0.9)
        ket = lens_documents.ket_reference(row)
        self.assertEqual(ket["type"], "contact_lens_document")
        self.assertEqual(ket["ket_ref"], "KET-77")
        self.assertNotIn("-3.75", str(ket))
        self.assertNotIn("customer_id", ket)


# ---------------------------------------------------------------------------
# routes, on MariaDB
# ---------------------------------------------------------------------------

LENS = {"product_id": 1015, "product_name": "Precision1 (30 pack)",
        "product_special_price_eur": 15.11, "lens_type": "SPHERICAL",
        "param_mode": "MATRIX",
        "min_boxes_single_eye": 12, "min_boxes_both_per_eye": 6}

VARIANTS = [
    {"variant_id": 41, "sph": "-3.75", "cyl": None, "axis": None,
     "add_power": None, "base_curve": "8.30", "diameter": "14.20",
     "color_code": "", "color_name": None},
    {"variant_id": 42, "sph": "-2.50", "cyl": None, "axis": None,
     "add_power": None, "base_curve": "8.30", "diameter": "14.20",
     "color_code": "", "color_name": None},
]


def _load_upload(get_db, ops_auth, call_model, site):
    pkg_name = "lu_pkg"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [REPO]
    sys.modules[pkg_name] = pkg

    def sub(name, **attrs):
        mod = types.ModuleType(pkg_name + "." + name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[pkg_name + "." + name] = mod
        setattr(pkg, name, mod)
        return mod

    def real(name):
        spec = importlib.util.spec_from_file_location(
            pkg_name + "." + name, os.path.join(REPO, "%s.py" % name))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        setattr(pkg, name, mod)
        spec.loader.exec_module(mod)
        return mod

    sub("db", get_db=get_db)
    sub("ops", _require_ops_auth=ops_auth)

    class ModelError(Exception):
        pass

    class ModelUnavailable(ModelError):
        pass

    sub("ai_client", ModelError=ModelError, ModelUnavailable=ModelUnavailable,
        call_model=call_model)
    real("catalogue")
    real("dev_defects")
    real("lens_order")
    real("lens_rx")
    docs = real("lens_documents")

    def _lens_choices(cursor, lens):
        return pkg.lens_order.selectable(VARIANTS)

    sub("models", current_site=lambda: site[0],
        _released_or_previewed_lens=lambda cursor, pid: (
            (LENS, False) if str(pid) == "1015" else (None, False)),
        _lens_choices=_lens_choices, _minimums_waived=lambda cart=None: False)
    up = real("lens_upload")
    return up, docs


@unittest.skipUnless(AVAILABLE, "no MariaDB test database (see scripts/setup_test_db.sh)")
class UploadRoutes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Blueprint, Flask
        cls.db = _connect()
        cls.authorised = [False]
        cls.site = ["optiwar.com"]
        cls.answers = []
        cls.calls = []

        def call_model(**kw):
            cls.calls.append(kw)
            answer = cls.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            resp = types.SimpleNamespace(model="gpt-4o-test")
            resp.choices = [types.SimpleNamespace(
                message=types.SimpleNamespace(content=answer))]
            return resp

        cls.up, cls.docs = _load_upload(lambda: cls.db, lambda: cls.authorised[0],
                                        call_model, cls.site)
        cls.tmp = tempfile.mkdtemp(prefix="owlens")
        os.makedirs(os.path.join(cls.tmp, "app"))
        cls.app = Flask(__name__, root_path=os.path.join(cls.tmp, "app"))
        cls.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        bp = Blueprint("main", __name__)
        cls.up.register(bp)
        cls.app.register_blueprint(bp)
        cls.client = cls.app.test_client()
        cursor = cls.db.cursor()
        for _, ddl in cls.docs.TABLES:
            cursor.execute(ddl)
        cls.db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.authorised[0] = False
        self.site[0] = "optiwar.com"
        del self.answers[:]
        del self.calls[:]
        with self.client.session_transaction() as sess:
            sess.clear()

    def _sign_in(self, customer_id=498):
        with self.client.session_transaction() as sess:
            sess["user_id"] = customer_id

    def _post(self, data=None, name="rx.jpg", product_id="1015"):
        return self.client.post(
            "/contact-lenses/rx-upload",
            data={"product_id": product_id,
                  "document": (io.BytesIO(data if data is not None else _jpeg()), name)},
            content_type="multipart/form-data")

    def _row(self, document_id):
        cursor = self.db.cursor()
        return self.docs.by_id(cursor, document_id)

    def _audit(self, document_id):
        cursor = self.db.cursor()
        cursor.execute("SELECT action, actor, detail FROM contact_lens_document_audit "
                       "WHERE document_id=%s ORDER BY id", (document_id,))
        return cursor.fetchall()

    def test_dot_in_has_no_upload_route(self):
        self.site[0] = "in.optiwar.com"
        self._sign_in()
        self.assertEqual(self._post().status_code, 404)
        self.assertEqual(self.calls, [])

    def test_a_visitor_must_sign_in_and_nothing_is_stored(self):
        r = self._post()
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["code"], "RX_UPLOAD_SIGN_IN")
        self.assertEqual(self.calls, [])

    def _files(self):
        root = os.path.join(self.tmp, "secure_uploads", "lens_rx")
        return sorted(os.listdir(root)) if os.path.isdir(root) else []

    def test_a_refused_file_never_reaches_the_provider_or_the_disk(self):
        self._sign_in()
        before = self._files()
        cursor = self.db.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM contact_lens_documents")
        rows = cursor.fetchone()["n"]
        r = self._post(b"<html>" + b"\x00" * 5000, name="rx.jpg")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["code"], "RX_UPLOAD_REJECTED")
        self.assertEqual(self.calls, [])
        self.assertEqual(self._files(), before)
        cursor.execute("SELECT COUNT(*) AS n FROM contact_lens_documents")
        self.assertEqual(cursor.fetchone()["n"], rows)

    def test_a_readable_upload_is_stored_read_validated_and_proposed(self):
        self._sign_in()
        self.answers.append('{"right": {"sph": "-3.75", "bc": "8.3"}, '
                            '"left": {"sph": "-2.50"}, "confidence": 0.88}')
        r = self._post(_pdf(pages=2), name="rx.pdf")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["eyes"]["right"]["sph"], "-3.75")
        self.assertEqual(body["eyes"]["left"]["sph"], "-2.50")
        self.assertEqual(sorted(body["applied_eyes"]), ["left", "right"])
        self.assertEqual(body["confidence"], 0.88)
        # provider asked once, with the default workload and two pages inline
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["workload"], "openai_vision")
        self.assertEqual(len(self.calls[0]["messages"][0]["content"]), 3)
        # row: typed metadata, files outside the web root, one per page
        row = self._row(body["document_id"])
        self.assertEqual(row["status"], "PARSED")
        self.assertEqual(row["customer_id"], 498)
        self.assertEqual(row["original_kind"], "pdf")
        self.assertEqual(row["pages"], 2)
        self.assertEqual(row["provider"], "openai_vision")
        self.assertEqual(row["model"], "gpt-4o-test")
        self.assertEqual(row["retain_until"],
                         lens_rx.retain_until(row["created_at"]))
        for page in range(2):
            self.assertTrue(os.path.exists(os.path.join(
                self.tmp, "secure_uploads", "lens_rx",
                self.docs.stored_name(body["document_id"], page))))
        self.assertEqual([a["action"] for a in self._audit(body["document_id"])],
                         ["UPLOADED", "PARSED"])
        # the proposal is parked for a reload, marked as an upload, tied to the row
        with self.client.session_transaction() as sess:
            parked = sess[lens_rx.PROPOSAL_SESSION_KEY]
        self.assertEqual(parked["source"], "upload")
        self.assertEqual(parked["document_id"], body["document_id"])
        self.assertEqual(parked["form"]["right_sph"], "-3.75")
        self.assertEqual(parked["form"]["left_sph"], "-2.50")
        self.assertEqual(parked["form"]["right_boxes"], "6")

    def test_the_provider_is_switched_by_configuration_not_code(self):
        self._sign_in()
        self.app.config["LENS_RX_VISION_PROVIDER"] = "deepseek_vision"
        try:
            self.answers.append('{"right": {"sph": "-3.75"}}')
            r = self._post()
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self.calls[0]["workload"], "deepseek_vision")
            self.assertEqual(self._row(r.get_json()["document_id"])["provider"],
                             "deepseek_vision")
        finally:
            self.app.config.pop("LENS_RX_VISION_PROVIDER")

    def test_a_provider_failure_keeps_the_upload_and_says_so(self):
        self._sign_in()
        self.answers.append(sys.modules["lu_pkg.ai_client"].ModelUnavailable("down"))
        r = self._post()
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertEqual(body["code"], "RX_UPLOAD_PROVIDER_FAILED")
        cursor = self.db.cursor()
        cursor.execute("SELECT document_id, status, refusal FROM contact_lens_documents "
                       "ORDER BY document_id DESC LIMIT 1")
        row = cursor.fetchone()
        self.assertEqual(row["status"], "UNREADABLE")
        self.assertEqual(row["refusal"], "ModelUnavailable")
        self.assertEqual([a["action"] for a in self._audit(row["document_id"])],
                         ["UPLOADED", "PARSE_FAILED"])
        with self.client.session_transaction() as sess:
            self.assertNotIn(lens_rx.PROPOSAL_SESSION_KEY, sess)

    def test_an_unreadable_answer_is_a_422_with_no_proposal(self):
        self._sign_in()
        self.answers.append("Sorry, this is a photo of a cat.")
        r = self._post()
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()["code"], "RX_UPLOAD_UNREADABLE")
        with self.client.session_transaction() as sess:
            self.assertNotIn(lens_rx.PROPOSAL_SESSION_KEY, sess)

    def test_a_reading_the_matrix_does_not_hold_is_incompatible(self):
        self._sign_in()
        self.answers.append('{"right": {"sph": "-9.00"}, "left": {"sph": "-2.50"}}')
        r = self._post()
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()["code"], "RX_UPLOAD_INCOMPATIBLE")
        cursor = self.db.cursor()
        cursor.execute("SELECT status, refusal, parsed_json FROM contact_lens_documents "
                       "ORDER BY document_id DESC LIMIT 1")
        row = cursor.fetchone()
        self.assertEqual(row["status"], "INCOMPATIBLE")
        self.assertTrue(row["refusal"])
        with self.client.session_transaction() as sess:
            self.assertNotIn(lens_rx.PROPOSAL_SESSION_KEY, sess)

    def test_an_unknown_product_is_refused_before_reading(self):
        self._sign_in()
        r = self._post(product_id="999999")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.calls, [])

    def _uploaded(self, customer_id=498):
        self._sign_in(customer_id)
        self.answers.append('{"right": {"sph": "-3.75"}, "left": {"sph": "-2.50"}}')
        r = self._post()
        self.assertEqual(r.status_code, 200)
        return r.get_json()["document_id"]

    def test_ownership_is_the_row_joined_to_the_customer(self):
        document_id = self._uploaded()
        cursor = self.db.cursor()
        self.assertIsNotNone(self.docs.owned(cursor, 498, document_id))
        self.assertIsNone(self.docs.owned(cursor, 499, document_id))
        self.assertIsNone(self.docs.owned(cursor, 498, "x"))
        self.assertIsNone(self.docs.owned(cursor, None, document_id))
        # confirmation is the owner's alone
        self.assertFalse(self.docs.mark_confirmed(cursor, document_id, 499))
        self.assertTrue(self.docs.mark_confirmed(cursor, document_id, 498))
        self.db.commit()
        self.assertEqual(self._row(document_id)["status"], "CONFIRMED")

    def test_ops_routes_fail_closed(self):
        document_id = self._uploaded()
        for method, path in (("get", "/api/ops/lens-documents/%d" % document_id),
                             ("post", "/api/ops/lens-documents/%d/link" % document_id),
                             ("post", "/api/ops/lens-documents/%d/ket-ref" % document_id)):
            self.assertEqual(getattr(self.client, method)(path).status_code, 401, path)
        self.assertEqual(self.client.get(
            "/ops/lens-documents/%d/file/0" % document_id).status_code, 403)
        self.assertEqual([a["action"] for a in self._audit(document_id)],
                         ["UPLOADED", "PARSED"])

    def test_ops_see_facts_not_powers_and_download_by_signed_link(self):
        document_id = self._uploaded()
        self.authorised[0] = True
        with self.client.session_transaction() as sess:
            sess["user_email"] = "ops@optiwar.com"
        r = self.client.get("/api/ops/lens-documents/%d" % document_id)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["document"]["status"], "PARSED")
        self.assertNotIn("parsed_json", body["document"])
        self.assertNotIn("-3.75", r.get_data(as_text=True))
        self.assertEqual(body["ket"]["document_id"], document_id)

        r = self.client.post("/api/ops/lens-documents/%d/link" % document_id)
        self.assertEqual(r.status_code, 200)
        links = r.get_json()["links"]
        self.assertEqual(len(links), 1)
        self.assertIn("exp=", links[0])
        self.assertIn("sig=", links[0])

        # the link works, once signed in or not — the signature is the key
        with self.client.session_transaction() as sess:
            sess.clear()
        self.authorised[0] = False
        r = self.client.get(links[0])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/jpeg")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(self.docs.kind_of(r.data), "jpeg")
        r.close()
        # tampering: another page, another document, a wrong signature
        self.assertEqual(self.client.get(links[0].replace("/file/0", "/file/1"))
                         .status_code, 403)
        self.assertEqual(self.client.get(links[0].replace(
            "/lens-documents/%d/" % document_id,
            "/lens-documents/%d/" % (document_id + 1000))).status_code, 403)
        self.assertEqual(self.client.get(links[0][:-4] + "0000").status_code, 403)
        actions = [(a["action"], a["actor"]) for a in self._audit(document_id)]
        self.assertEqual(actions[2], ("LINK_ISSUED", "ops@optiwar.com"))
        self.assertEqual(actions[3], ("DOWNLOADED", "signed_link"))
        self.assertEqual(len(actions), 4)

    def test_a_signed_link_expires(self):
        document_id = self._uploaded()
        with self.app.test_request_context():
            past = self.up.signed_path(document_id, 0, now=1_000_000)
            self.assertFalse(self.up.link_valid(document_id, 0, "abc", "x"))
            self.assertTrue(self.up.link_valid(
                document_id, 0, 1_000_000 + self.up.LINK_TTL_SECONDS,
                past.split("sig=")[1], now=1_000_000 + 10))
            self.assertFalse(self.up.link_valid(
                document_id, 0, 1_000_000 + self.up.LINK_TTL_SECONDS,
                past.split("sig=")[1], now=1_000_000 + self.up.LINK_TTL_SECONDS + 1))
        self.assertEqual(self.client.get(past).status_code, 403)

    def test_the_file_route_never_leaves_its_directory(self):
        for path in ("/ops/lens-documents/1/file/../../etc/passwd",
                     "/ops/lens-documents/1/file/%2e%2e",
                     "/ops/lens-documents/abc/file/0"):
            self.assertIn(self.client.get(path).status_code, (403, 404), path)

    def test_a_ket_reference_is_recorded_validated_and_audited(self):
        document_id = self._uploaded()
        self.authorised[0] = True
        r = self.client.post("/api/ops/lens-documents/%d/ket-ref" % document_id,
                             json={"ket_ref": "KET-2026-000123"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["ket"]["ket_ref"], "KET-2026-000123")
        r = self.client.post("/api/ops/lens-documents/%d/ket-ref" % document_id,
                             json={"ket_ref": "<script>"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/ops/lens-documents/%d/ket-ref" % (document_id + 1000),
                             json={"ket_ref": "KET-1"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._audit(document_id)[-1]["action"], "KET_REF")
        self.assertEqual(self._row(document_id)["ket_ref"], "KET-2026-000123")

    def test_purge_removes_the_files_and_keeps_the_row_and_its_trail(self):
        document_id = self._uploaded()
        cursor = self.db.cursor()
        cursor.execute("UPDATE contact_lens_documents SET retain_until=%s "
                       "WHERE document_id=%s", (datetime.date(2020, 1, 1), document_id))
        self.db.commit()
        path = os.path.join(self.tmp, "secure_uploads", "lens_rx",
                            self.docs.stored_name(document_id, 0))
        self.assertTrue(os.path.exists(path))
        with self.app.app_context():
            purged = self.up.purge_expired(self.db)
            self.assertIn(document_id, purged)
            self.assertEqual(self.up.purge_expired(self.db), [])
        self.assertFalse(os.path.exists(path))
        row = self._row(document_id)
        self.assertEqual(row["status"], "PURGED")
        self.assertIsNone(row["stored_name"])
        self.assertIsNone(row["parsed_json"])
        self.assertIsNotNone(row["purged_at"])
        self.assertEqual(row["sha256"] and len(row["sha256"]), 64)
        self.assertEqual(self._audit(document_id)[-1]["action"], "PURGED")
        self.assertIsNone(self.docs.owned(cursor, 498, document_id))
        self.authorised[0] = True
        self.assertEqual(self.client.post(
            "/api/ops/lens-documents/%d/link" % document_id).status_code, 404)


class _DictSession(dict):
    modified = False


class Wiring(unittest.TestCase):

    def _read(self, rel):
        with open(os.path.join(REPO, rel)) as fh:
            return fh.read()

    def test_the_upload_enters_the_cards_through_the_one_engine(self):
        cards = self._read("templates/_lens_eye_cards.html")
        self.assertIn('data-role="upload-panel"', cards)
        self.assertIn("fetch('/contact-lenses/rx-upload'", cards)
        self.assertIn("applyPrescription(data.eyes, 'upload'", cards)
        self.assertEqual(cards.count("function applyPrescription("), 1)
        # success is said only after the read-back matched; a mismatch is a
        # distinguished defect and no provenance claim
        apply = cards[cards.index("function applyPrescription("):]
        apply = apply[:apply.index("\n  }\n")]
        self.assertIn("if (source === 'upload') {", apply)
        upload = apply[apply.index("if (source === 'upload') {"):]
        self.assertLess(upload.index("RX_UPLOAD_STATE_MISMATCH"),
                        upload.index("rxSource.value = 'UPLOADED_CONFIRMED'"))
        self.assertIn("if (reused) { reused.value = ''; }", upload)
        for code in ("RX_UPLOAD_STATE_MISMATCH", "RX_UPLOAD_APPLY_FAILED",
                     "RX_UPLOAD_FETCH_FAILED", "RX_UPLOAD_BAD_RESPONSE"):
            self.assertIn(code, cards)
        self.assertNotIn("Prescription upload is being prepared", cards)

    def test_choosing_a_file_starts_the_reading_and_the_button_only_repeats_it(self):
        cards = self._read("templates/_lens_eye_cards.html")
        self.assertIn("uploadFile.addEventListener('change'", cards)
        self.assertIn("uploadRead.addEventListener('click', readUpload)", cards)
        self.assertEqual(cards.count("fetch('/contact-lenses/rx-upload'"), 1)
        read = cards[cards.index("function readUpload()"):]
        # a reading already in flight is not started twice
        self.assertLess(read.index("if (uploadRead.disabled) { return; }"),
                        read.index("uploadRead.disabled = true;"))
        self.assertNotIn("Read my prescription", cards)

    def test_a_new_notice_hides_a_stale_server_rendered_proposal_note(self):
        cards = self._read("templates/_lens_eye_cards.html")
        note = cards[cards.index("function loadedNote("):]
        note = note[:note.index("\n  }\n")]
        self.assertIn("role(form, 'ai-note')", note)
        self.assertIn("stale.hidden = true", note)

    def test_a_parked_proposal_prefills_the_page_once_and_stays_for_provenance(self):

        tree = ast.parse(self._read("models.py"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "_proposal_prefill")
        ns = {"session": _DictSession(), "lens_rx": _load("lens_rx")}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "models.py", "exec"), ns)
        prefill = ns["_proposal_prefill"]
        proposal = {"product_id": "1015", "form": {"right_sph": "-6.00"}}
        self.assertIs(prefill(proposal), proposal)          # first page: shown
        self.assertTrue(proposal["shown"])
        self.assertTrue(ns["session"].modified)
        self.assertIsNone(prefill(proposal))                # later page: not again
        self.assertIsNone(prefill(None))
        # an upload's proposal is parked already shown: the browser applied it
        self.assertIn('"shown": True', self._read("lens_upload.py"))
        models = self._read("models.py")
        self.assertIn("proposal = _proposal_prefill(proposal)", models)

    def test_the_server_vouches_for_upload_provenance_only_against_its_own_row(self):
        models = self._read("models.py")
        self.assertIn("lens_upload.register(bp)", models)
        self.assertIn("lens_documents.owned(cursor, session.get('user_id')", models)
        self.assertIn("proposal.get('source') == 'upload'", models)
        self.assertIn("lens_documents.mark_confirmed(cursor, document_id", models)
        self.assertIn("lens_documents.AUD_CONFIRMED", models)

    def test_schema_deploy_manifest_and_report_know_the_new_tables(self):
        self.assertIn("contact_lens_documents",
                      [t for t, _ in _load("contact_lens").TABLES])
        deploy = self._read("deploy/deploy.py")
        self.assertIn('"lens_documents.py"', deploy)
        self.assertIn('"lens_upload.py"', deploy)
        report = self._read("reports/lens_report_section.py")
        for key in ("doc_uploaded", "doc_unread", "doc_confirmed", "doc_purge_due"):
            self.assertIn(key, report)
        self.assertIn("Prescription uploads (last", report)


if __name__ == "__main__":
    unittest.main()
