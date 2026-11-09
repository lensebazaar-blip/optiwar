"""The trademark / copyright holder notice form (ip_notice.py): what a notice
must contain, the reference and the dates the Terms promise, the stored row,
and one mail to Optiwar with a copy to the Rights Holder. No database."""
import datetime
import importlib.util
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _plain():
    spec = importlib.util.spec_from_file_location("ip_notice_plain", os.path.join(REPO, "ip_notice.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def good(**kw):
    form = {"holder_name": "Asha Rao", "company": "Example Eyewear Pvt Ltd", "designation": "Brand counsel",
            "email": "asha@example.com", "phone": "+91 98xxxxxxx", "address": "Mumbai",
            "rights_type": "trademark", "mark_or_work": "EXAMPLE (word mark)", "registration": "TM 1234567",
            "listing_urls": "https://optiwar.in/categories/spectacles-frame/ce-i?pid=379",
            "reason": "The listing uses our registered word mark on goods we did not supply.",
            "action_requested": "withdraw", "action_detail": "", "declaration": "1", "hold_agreed": "1"}
    form.update(kw)
    return form


class Parsing(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.ipn = _plain()

    def test_a_complete_notice_passes_and_is_cleaned(self):
        data, errors = self.ipn.parse(good(holder_name="  Asha   Rao "))
        self.assertEqual(errors, {})
        self.assertEqual(data["holder_name"], "Asha Rao")
        self.assertTrue(data["declaration"] and data["hold_agreed"])

    def test_the_terms_list_of_required_contents_is_enforced(self):
        for field, value in (("holder_name", ""), ("email", "nope"), ("rights_type", "patent"),
                             ("mark_or_work", ""), ("listing_urls", "the blue one"), ("reason", "copied"),
                             ("action_requested", ""), ("declaration", ""), ("hold_agreed", "")):
            _d, errors = self.ipn.parse(good(**{field: value}))
            self.assertIn(field, errors, field)
        _d, errors = self.ipn.parse(good(action_requested="other", action_detail=""))
        self.assertIn("action_detail", errors)

    def test_dates_follow_the_terms(self):
        received = datetime.datetime(2026, 11, 7, 15, 30)      # a Saturday
        ack_due, hold_until = self.ipn.dates(received)
        self.assertEqual(ack_due, datetime.date(2026, 11, 16))   # 7 working days, Sundays skipped
        self.assertEqual(hold_until, ack_due + datetime.timedelta(days=90))

    def test_the_reference_is_dated_and_stable(self):
        now = datetime.datetime(2026, 11, 7, 15, 30, 1, 5)
        ref = self.ipn.reference(now, "asha@example.com")
        self.assertTrue(ref.startswith("IPN-20261107-"))
        self.assertEqual(ref, self.ipn.reference(now, "asha@example.com"))
        self.assertNotEqual(ref, self.ipn.reference(now, "other@example.com"))

    def test_the_text_both_parties_receive_carries_everything(self):
        data, _ = self.ipn.parse(good())
        text = self.ipn.notice_text(data, "IPN-20261107-ABCD", datetime.date(2026, 11, 16), datetime.date(2027, 2, 14),
                                    received=datetime.datetime(2026, 11, 7, 15, 30))
        for needle in ("IPN-20261107-ABCD", "Asha Rao", "EXAMPLE (word mark)", "TM 1234567",
                       "https://optiwar.in/categories/spectacles-frame/ce-i?pid=379",
                       "Withdraw the product from sale", "Monday, 16 November 2026", "Sunday, 14 February 2027",
                       "New Delhi and Gurugram", "90-day hold period"):
            self.assertIn(needle, text)

    def test_store_never_raises_and_the_ddl_is_additive(self):
        class Bad:
            def execute(self, *a):
                raise RuntimeError("no table")
        data, _ = self.ipn.parse(good())
        self.assertFalse(self.ipn.store(Bad(), "IPN-1", data, datetime.date(2026, 11, 16), datetime.date(2027, 2, 14)))
        self.assertFalse(self.ipn.mark_mailed(Bad(), "IPN-1"))
        ddl = dict(self.ipn.TABLES)["ip_notices"]
        self.assertIn("CREATE TABLE IF NOT EXISTS", ddl)
        self.assertNotIn("ALTER", ddl.upper())


class Route(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Blueprint, Flask
        pkg_name = "ipn_pkg"
        pkg = types.ModuleType(pkg_name); pkg.__path__ = [REPO]; sys.modules[pkg_name] = pkg
        cls.mails, cls.rows = [], []

        class Cursor:
            def execute(self, sql, args=None):
                if not cls.db_ok[0]:
                    raise RuntimeError("database is away")
                cls.rows.append((sql, args))

        class DB:
            def cursor(self):
                return Cursor()

            def commit(self):
                pass

        db = types.ModuleType(pkg_name + ".db"); db.get_db = lambda: DB(); sys.modules[db.__name__] = db; pkg.db = db
        mail = types.ModuleType(pkg_name + ".notice_mail")

        cls.mail_ok, cls.db_ok = [True], [True]

        def send_ip_notice(ref, email, name, text, holder_text=None, recipients=None):
            if not cls.mail_ok[0]:
                return False
            cls.mails.append((ref, email, name, text, holder_text, recipients)); return True
        mail.send_ip_notice = send_ip_notice; sys.modules[mail.__name__] = mail; pkg.notice_mail = mail
        spec = importlib.util.spec_from_file_location(pkg_name + ".ip_notice", os.path.join(REPO, "ip_notice.py"))
        mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)
        cls.ipn = mod
        import shutil
        import tempfile
        cls.tmp = tempfile.mkdtemp(prefix="ipn")
        with open(os.path.join(cls.tmp, "base.html"), "w", encoding="utf-8") as fh:
            fh.write("<!doctype html><title>{% block title %}{% endblock %}</title>{% block content %}{% endblock %}")
        for name in ("ip_notice.html", "_pagenav.html"):
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

        @bp.route("/terms_and_conditions")
        def terms_and_conditions():
            return ""

        @bp.route("/privacy_policy")
        def privacy_policy():
            return ""
        cls.app.register_blueprint(bp)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        del self.mails[:]
        del self.rows[:]
        self.mail_ok[0] = self.db_ok[0] = True

    def test_a_notice_that_was_neither_stored_nor_mailed_is_not_acknowledged(self):
        with self.client.session_transaction() as sess:
            sess[self.ipn.SESSION_COUNT_KEY] = 0
        self.mail_ok[0] = self.db_ok[0] = False
        r = self.client.post("/ip-notice", data=good())
        self.assertEqual(r.status_code, 503)
        self.assertIn(b"nothing has been sent", r.data)
        self.assertNotIn(b"Notice received", r.data)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get(self.ipn.SESSION_COUNT_KEY), 0)  # the retry is not counted against them
        self.mail_ok[0] = True                                          # mailed but not stored is still a receipt
        self.assertEqual(self.client.post("/ip-notice", data=good()).status_code, 200)

    def test_a_product_code_is_accepted_in_place_of_a_url(self):
        r = self.client.post("/ip-notice", data=good(listing_urls="Product code AN12 and CE-I-379"))
        self.assertEqual(r.status_code, 200, r.data[:200])
        r = self.client.post("/ip-notice", data=good(listing_urls="your blue frame"))
        self.assertEqual(r.status_code, 400)

    def test_the_form_is_served(self):
        r = self.client.get("/ip-notice")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'name="listing_urls"', r.data)
        self.assertIn(b'name="hold_agreed"', r.data)

    def test_a_good_notice_is_stored_mailed_to_both_and_acknowledged(self):
        r = self.client.post("/ip-notice", data=good())
        self.assertEqual(r.status_code, 200, r.data[:300])
        self.assertIn(b"IPN-", r.data)
        self.assertIn(b"asha@example.com", r.data)
        self.assertEqual(len(self.mails), 1)
        ref, email, name, text, holder_text, recipients = self.mails[0]
        self.assertEqual((email, name), ("asha@example.com", "Asha Rao"))
        self.assertIn(ref, text)
        self.assertEqual(tuple(recipients), ("admin@optiwar.com", "admin@lensbazaar.com"))
        self.assertIn("hold any further action until you hear from us", holder_text)
        self.assertIn("Dear Asha Rao", holder_text)
        self.assertIn(text, holder_text)
        inserts = [a for sql, a in self.rows if sql and sql.startswith("INSERT INTO ip_notices")]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][0], ref)
        self.assertTrue(any(sql.startswith("UPDATE ip_notices SET mail_sent") for sql, _a in self.rows))

    def test_an_incomplete_notice_is_refused_with_the_fields_named(self):
        r = self.client.post("/ip-notice", data=good(listing_urls="the blue one", hold_agreed=""))
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"listing URL", r.data)
        self.assertEqual(self.mails, [])

    def test_bots_and_floods_are_refused(self):
        r = self.client.post("/ip-notice", data=good(website_url="http://spam"))
        self.assertEqual(r.status_code, 400)
        with self.client.session_transaction() as sess:
            sess[self.ipn.SESSION_COUNT_KEY] = self.ipn.MAX_PER_SESSION
        r = self.client.post("/ip-notice", data=good())
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.mails, [])


class Wiring(unittest.TestCase):

    def test_registered_linked_and_deployed(self):
        models = open(os.path.join(REPO, "models.py"), encoding="utf-8").read()
        self.assertIn("ip_notice.register(bp)", models)
        terms = open(os.path.join(REPO, "templates", "terms-and-conditions.html"), encoding="utf-8").read()
        self.assertIn("url_for('main.ip_notice')", terms)
        base = open(os.path.join(REPO, "templates", "base.html"), encoding="utf-8").read()
        self.assertIn("url_for('main.ip_notice')", base)
        deploy = open(os.path.join(REPO, "deploy", "deploy.py"), encoding="utf-8").read()
        new_block = deploy[deploy.index("NEW_IN_RELEASE = ("):]
        new_block = new_block[:new_block.index('")\n') + 2]
        self.assertIn('"ip_notice.py"', new_block)
        self.assertIn('"templates/ip_notice.html"', new_block)
        dep_block = deploy[deploy.index("DEPLOY_SET = ("):deploy.index("NEW_IN_RELEASE = (")]
        self.assertIn('"ip_notice.py"', dep_block)
        self.assertIn('"templates/ip_notice.html"', dep_block)
        self.assertIn("ip_notice_module().TABLES", deploy)
        # production's mail.py is outside the deploy set: the senders live in a module that ships
        self.assertNotIn("send_ip_notice", open(os.path.join(REPO, "mail.py"), encoding="utf-8").read())
        self.assertIn("from .notice_mail import send_ip_notice", open(os.path.join(REPO, "ip_notice.py"), encoding="utf-8").read())
        mail = open(os.path.join(REPO, "notice_mail.py"), encoding="utf-8").read()
        dep_block = deploy[deploy.index("DEPLOY_SET = ("):deploy.index("NEW_IN_RELEASE = (")]
        self.assertIn('"notice_mail.py"', dep_block)
        self.assertNotIn('"mail.py"', dep_block.replace('"notice_mail.py"', ''))
        self.assertIn("def send_ip_notice(ref, holder_email, holder_name, text, holder_text=None, recipients=None)", mail)
        self.assertIn("server.send_message(internal)", mail)
        self.assertIn("server.send_message(holder)", mail)
        gateway = open(os.path.join(REPO, "chat_gateway.py"), encoding="utf-8").read()
        self.assertIn("ip_notice.prompt_section()", gateway)

    def test_the_assistant_is_told_to_be_courteous_and_point_to_the_form(self):
        section = _plain().prompt_section()
        for needle in ("courteous", "/ip-notice", "not a marketplace", "hold any further action",
                       "7 working days", "90 days", "admin@optiwar.com"):
            self.assertIn(needle, section)
        self.assertNotIn("lensbazaar", section)


if __name__ == "__main__":
    unittest.main()
