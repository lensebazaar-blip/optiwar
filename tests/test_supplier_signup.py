"""Become a supplier (supplier_signup.py): the GSTIN check, what an application
must contain, the reference, the letter, the stored row, the two mails. No database."""
import datetime
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _plain():
    spec = importlib.util.spec_from_file_location("supplier_signup_plain", os.path.join(REPO, "supplier_signup.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GSTIN = "06AAHCK4567A1ZP"   # Krishna Ecommerce Technologies, Haryana


def good(**kw):
    form = {"business_name": "Example Optics Pvt Ltd", "business_type": "manufacturer", "contact_name": "Ravi Mehta",
            "designation": "Director", "email": "ravi@example.com", "phone": "+91 98765 43210",
            "website": "https://example.com", "gstin": GSTIN, "address": "Plot 12, Sector 7, IMT Manesar",
            "city": "Gurugram", "state": "Haryana", "pincode": "122050", "factory_address": "",
            "categories": ["frames", "sunglasses"], "brands": "Example, Exa-Lite", "capacity": "20,000 frames / month, MOQ 300",
            "years_in_business": "12", "message": "ISO 9001 certified.", "accept": "1"}
    form.update(kw)
    return form


class _Form(dict):
    def getlist(self, key):
        v = self.get(key, [])
        return v if isinstance(v, list) else [v]


class Gstin(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.sup = _plain()

    def test_a_real_gstin_passes_and_yields_state_and_pan(self):
        g, state, pan, err = self.sup.parse_gstin(" 06aahck4567a1zp ")
        self.assertEqual((g, state, pan, err), (GSTIN, "Haryana", "AAHCK4567A", ""))

    def test_shape_state_code_and_checksum_are_checked(self):
        self.assertIn("15 characters", self.sup.parse_gstin("06AAHCK4567A1Z")[3])
        self.assertIn("state code", self.sup.parse_gstin("99AAHCK4567A1ZP".replace("99", "45"))[3])
        self.assertIn("checksum", self.sup.parse_gstin("06AAHCK4567A1ZQ")[3])
        self.assertIn("required", self.sup.parse_gstin("")[3])
        self.assertEqual(self.sup.gstin_checksum(GSTIN[:14]), "P")


class Parsing(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.sup = _plain()

    def test_a_complete_application_passes(self):
        data, errors = self.sup.parse(_Form(good()))
        self.assertEqual(errors, {})
        self.assertEqual(data["gst_state"], "Haryana")
        self.assertEqual(data["categories"], ["frames", "sunglasses"])

    def test_required_fields_and_the_gst_state_match(self):
        for field, value in (("business_name", ""), ("business_type", "shop"), ("contact_name", ""), ("email", "x"),
                             ("phone", "12"), ("gstin", "bad"), ("address", "x"), ("city", ""), ("accept", "")):
            _d, errors = self.sup.parse(_Form(good(**{field: value})))
            self.assertIn(field, errors, field)
        _d, errors = self.sup.parse(_Form(good(state="Delhi")))
        self.assertIn("registered in Haryana", errors["state"])
        _d, errors = self.sup.parse(_Form(good(categories=[])))
        self.assertIn("categories", errors)

    def test_reference_letter_and_text(self):
        now = datetime.datetime(2026, 11, 7, 16, 0)
        ref = self.sup.reference(now, "ravi@example.com")
        self.assertTrue(ref.startswith("SUP-20261107-"))
        data, _ = self.sup.parse(_Form(good()))
        text = self.sup.application_text(data, ref, received=now)
        for needle in (ref, "Example Optics Pvt Ltd", "Manufacturer / factory", GSTIN, "GST state (from the GSTIN): Haryana",
                       "PAN (from the GSTIN): AAHCK4567A", "Spectacle frames, Sunglasses", "20,000 frames / month"):
            self.assertIn(needle, text)
        review_by = self.sup.working_days_after(now, self.sup.REVIEW_WORKING_DAYS)
        self.assertEqual(review_by, datetime.date(2026, 11, 16))
        letter = self.sup.letter_text(data, ref, review_by, text)
        for needle in ("Dear Ravi Mehta", ref, "Monday, 16 November 2026", "GST registration certificate",
                       "not a marketplace", "Optiwar Sourcing Team", text):
            self.assertIn(needle, letter)

    def test_store_never_raises_and_the_ddl_is_additive(self):
        class Bad:
            def execute(self, *a):
                raise RuntimeError("no table")
        data, _ = self.sup.parse(_Form(good()))
        self.assertFalse(self.sup.store(Bad(), "SUP-1", data))
        ddl = dict(self.sup.TABLES)["supplier_applications"]
        self.assertIn("CREATE TABLE IF NOT EXISTS", ddl)
        self.assertNotIn("ALTER", ddl.upper())


class Route(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Blueprint, Flask
        pkg_name = "sup_pkg"
        pkg = types.ModuleType(pkg_name); pkg.__path__ = [REPO]; sys.modules[pkg_name] = pkg
        cls.mails, cls.rows = [], []

        class Cursor:
            def execute(self, sql, args=None):
                cls.rows.append((sql, args))

        class DB:
            def cursor(self):
                return Cursor()

            def commit(self):
                pass
        db = types.ModuleType(pkg_name + ".db"); db.get_db = lambda: DB(); sys.modules[db.__name__] = db; pkg.db = db
        mail = types.ModuleType(pkg_name + ".mail")

        def send_supplier_signup(ref, email, business, text, letter, recipients=None):
            cls.mails.append((ref, email, business, text, letter, recipients)); return True
        mail.send_supplier_signup = send_supplier_signup; sys.modules[mail.__name__] = mail; pkg.mail = mail
        spec = importlib.util.spec_from_file_location(pkg_name + ".supplier_signup", os.path.join(REPO, "supplier_signup.py"))
        mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)
        cls.sup = mod
        cls.tmp = tempfile.mkdtemp(prefix="sup")
        with open(os.path.join(cls.tmp, "base.html"), "w", encoding="utf-8") as fh:
            fh.write("<!doctype html><title>{% block title %}{% endblock %}</title>{% block content %}{% endblock %}")
        for name in ("supplier_signup.html", "_pagenav.html"):
            shutil.copy(os.path.join(REPO, "templates", name), os.path.join(cls.tmp, name))
        cls.app = Flask(__name__, template_folder=cls.tmp)
        cls.app.config.update(TESTING=True, SECRET_KEY="t")
        cls.app.jinja_env.add_extension("jinja2.ext.i18n")
        cls.app.jinja_env.install_null_translations(newstyle=True)
        bp = Blueprint("main", __name__)
        mod.register(bp)

        @bp.route("/")
        def index():
            return ""
        cls.app.register_blueprint(bp)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        del self.mails[:]
        del self.rows[:]

    def test_the_form_is_served(self):
        r = self.client.get("/become-a-supplier")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'name="gstin"', r.data)
        self.assertIn(b'name="categories"', r.data)

    def test_a_good_application_is_stored_mailed_and_confirmed(self):
        r = self.client.post("/become-a-supplier", data=good())
        self.assertEqual(r.status_code, 200, r.data[:300])
        self.assertIn(b"SUP-", r.data)
        self.assertIn(b"ravi@example.com", r.data)
        self.assertEqual(len(self.mails), 1)
        ref, email, business, text, letter, recipients = self.mails[0]
        self.assertEqual((email, business), ("ravi@example.com", "Example Optics Pvt Ltd"))
        self.assertEqual(tuple(recipients), ("admin@optiwar.com", "admin@lensbazaar.com"))
        self.assertIn("Dear Ravi Mehta", letter)
        inserts = [a for sql, a in self.rows if sql and sql.startswith("INSERT INTO supplier_applications")]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][0], ref)
        self.assertIn("Haryana", inserts[0])
        self.assertTrue(any(sql.startswith("UPDATE supplier_applications SET mail_sent") for sql, _a in self.rows))

    def test_a_bad_gstin_is_refused_with_the_reason(self):
        r = self.client.post("/become-a-supplier", data=good(gstin="06AAHCK4567A1ZQ"))
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"checksum", r.data)
        self.assertEqual(self.mails, [])

    def test_bots_and_floods_are_refused(self):
        self.assertEqual(self.client.post("/become-a-supplier", data=good(website_url="spam")).status_code, 400)
        with self.client.session_transaction() as sess:
            sess[self.sup.SESSION_COUNT_KEY] = self.sup.MAX_PER_SESSION
        self.assertEqual(self.client.post("/become-a-supplier", data=good()).status_code, 429)
        self.assertEqual(self.mails, [])


class Wiring(unittest.TestCase):

    def test_registered_linked_and_deployed(self):
        models = open(os.path.join(REPO, "models.py"), encoding="utf-8").read()
        self.assertIn("supplier_signup.register(bp)", models)
        base = open(os.path.join(REPO, "templates", "base.html"), encoding="utf-8").read()
        self.assertIn("url_for('main.supplier_signup')", base)
        deploy = open(os.path.join(REPO, "deploy", "deploy.py"), encoding="utf-8").read()
        dep_block = deploy[deploy.index("DEPLOY_SET = ("):deploy.index("NEW_IN_RELEASE = (")]
        new_block = deploy[deploy.index("NEW_IN_RELEASE = ("):]
        new_block = new_block[:new_block.index('")\n') + 2]
        for name in ('"supplier_signup.py"', '"templates/supplier_signup.html"', '"static/css/ow-forms.css"'):
            self.assertIn(name, dep_block, name)
            self.assertIn(name, new_block, name)
        self.assertIn("supplier_signup_module().TABLES", deploy)
        mail = open(os.path.join(REPO, "mail.py"), encoding="utf-8").read()
        self.assertIn("def send_supplier_signup(ref, supplier_email, business_name, text, letter, recipients=None)", mail)
        ipn = open(os.path.join(REPO, "templates", "ip_notice.html"), encoding="utf-8").read()
        self.assertIn("css/ow-forms.css", ipn)
        self.assertNotIn("ipn-", ipn)


if __name__ == "__main__":
    unittest.main()
