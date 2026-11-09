"""content_guard: every customer upload route is checked before anything is
kept; a flag leaves no image; what cannot be checked is not accepted."""
import importlib.util
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


class _Cursor:
    flags, blocked = [], [False]

    def execute(self, sql, args=None):
        self._row = None
        if sql.startswith("INSERT INTO upload_flags"):
            self.flags.append(args)
        elif "COUNT(*)" in sql:
            self._row = {"n": 1 if self.blocked[0] else 0}

    def fetchone(self):
        return self._row


class _Db:
    def cursor(self):
        return _Cursor()

    def commit(self):
        pass


class Screen(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Flask
        pkg_name = "cg_pkg"
        pkg = types.ModuleType(pkg_name); pkg.__path__ = [REPO]; sys.modules[pkg_name] = pkg
        db = types.ModuleType(pkg_name + ".db"); db.get_db = _Db; sys.modules[db.__name__] = db; pkg.db = db
        cls.calls, cls.answers = [], []

        def call_model(**kw):
            cls.calls.append(kw)
            a = cls.answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=a))])
        ai = types.ModuleType(pkg_name + ".ai_client"); ai.call_model = call_model; sys.modules[ai.__name__] = ai; pkg.ai_client = ai
        ld = types.ModuleType(pkg_name + ".lens_documents"); ld.provider_name = lambda c: "openai_vision"
        sys.modules[ld.__name__] = ld; pkg.lens_documents = ld
        cls.alerts = []

        def send_content_alert(to, subject, body):
            cls.alerts.append((to, subject, body)); return True
        nm = types.ModuleType(pkg_name + ".notice_mail"); nm.send_content_alert = send_content_alert
        sys.modules[nm.__name__] = nm; pkg.notice_mail = nm
        spec = importlib.util.spec_from_file_location(pkg_name + ".content_guard", os.path.join(REPO, "content_guard.py"))
        cls.cg = importlib.util.module_from_spec(spec); sys.modules[spec.name] = cls.cg; spec.loader.exec_module(cls.cg)
        cls.app = Flask(__name__)
        cls.app.config.update(TESTING=True, SECRET_KEY="t", CONTENT_GUARD_IN_TESTS=True)

    def setUp(self):
        del self.flags_ref()[:]
        del self.calls[:]
        del self.answers[:]
        del self.alerts[:]
        _Cursor.blocked[0] = False

    @staticmethod
    def flags_ref():
        return _Cursor.flags

    def _screen(self, door="chat"):
        with self.app.test_request_context("/", environ_overrides={"REMOTE_ADDR": "203.0.113.9"}):
            return self.cg.screen([b"img"], door, "a" * 64)

    def test_an_ordinary_image_passes_and_leaves_no_record(self):
        self.answers.append("none")
        self.assertIsNone(self._screen())
        self.assertEqual(_Cursor.flags, [])
        msg = self.calls[0]
        self.assertEqual(msg["max_tokens"], 8)
        self.assertEqual(msg["messages"][0]["content"][1]["image_url"]["detail"], "low")

    def test_each_category_is_refused_and_recorded_without_the_image(self):
        for category in self.cg.CATEGORIES:
            del _Cursor.flags[:]
            self.answers.append(category + ".")
            r = self._screen("return_photos")
            self.assertEqual((r.code, r.status), ("UPLOAD_BLOCKED", 422), category)
            self.assertIn("Cyber Crime India", r.message)
            row = _Cursor.flags[0]
            self.assertEqual(row[1:4], (category, "a" * 64, "203.0.113.9"))
            self.assertEqual(row[5], "return_photos")
            self.assertFalse(any(isinstance(v, bytes) for v in row))
            to, subject, body = self.alerts[-1]
            self.assertEqual(to, "admin@optiwar.com")
            self.assertIn(category, subject)
            for fact in ("return_photos", "203.0.113.9", "a" * 64, "cybercrime.gov.in", "was not stored"):
                self.assertIn(fact, body)

    def test_a_failed_alert_never_changes_the_refusal(self):
        self.answers.append("sexual")
        keep = sys.modules["cg_pkg.notice_mail"].send_content_alert

        def boom(*a):
            raise RuntimeError("smtp down")
        sys.modules["cg_pkg.notice_mail"].send_content_alert = boom
        try:
            self.assertEqual(self._screen().code, "UPLOAD_BLOCKED")
        finally:
            sys.modules["cg_pkg.notice_mail"].send_content_alert = keep

    def test_a_clean_or_unchecked_image_sends_no_alert(self):
        self.answers.extend(["none", "maybe"])
        self._screen()
        self._screen()
        self.assertEqual(self.alerts, [])

    def test_what_cannot_be_checked_is_not_accepted(self):
        self.answers.append(RuntimeError("provider down"))
        self.assertEqual(self._screen().code, "UPLOAD_CHECK_UNAVAILABLE")
        self.answers.append("I cannot help with that image")
        self.assertEqual(self._screen().code, "UPLOAD_CHECK_UNAVAILABLE")
        self.answers.append("")
        self.assertEqual(self._screen().status, 503)
        self.assertEqual(_Cursor.flags, [])

    def test_a_blocked_ip_costs_no_paid_call(self):
        _Cursor.blocked[0] = True
        self.assertEqual(self._screen().code, "UPLOAD_NOT_AVAILABLE")
        self.assertEqual(self.calls, [])

    def test_verdict_reading(self):
        v = self.cg.verdict
        self.assertEqual(v("None"), "none")
        self.assertEqual(v("sexual\n"), "sexual")
        self.assertIsNone(v("maybe"))
        self.assertIsNone(v(""))


class FaceScanUnreadable(Screen):
    """A file face_scan would keep but the check cannot open must not be stored unchecked."""

    def _run(self, validator_error):
        import ast
        import io
        source = open(os.path.join(REPO, "face_scan.py"), encoding="utf-8").read()
        fn = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "_screen_upload")
        ld = sys.modules["cg_pkg.lens_documents"]

        class Rejected(Exception):
            def __init__(self, code, message):
                super().__init__(message)
                self.code, self.message = code, message

        def normalise(data):
            raise Rejected("RX_UPLOAD_HEIC_UNSUPPORTED", "Please upload it as JPEG.")
        ld.Rejected, ld.normalise = Rejected, normalise
        scope = {"__package__": "cg_pkg", "__name__": "cg_pkg.face_scan",
                 "_valid_image": lambda f: ("heic", 5000, validator_error)}
        exec(ast.get_source_segment(source, fn), scope)
        upload = io.BytesIO(b"x" * 5000)
        with self.app.test_request_context("/"):
            result = scope["_screen_upload"](upload)
        self.assertEqual(upload.tell(), 0)
        return result

    def test_heic_is_refused_with_its_message_and_costs_no_call(self):
        r = self._run(None)
        self.assertEqual((r.code, r.message, r.status), ("RX_UPLOAD_HEIC_UNSUPPORTED", "Please upload it as JPEG.", 400))
        self.assertEqual(self.calls, [])

    def test_a_file_the_validator_refuses_is_left_to_it(self):
        self.assertIsNone(self._run("That file looks empty."))


class Wiring(unittest.TestCase):

    def _src(self, name):
        return open(os.path.join(REPO, name), encoding="utf-8").read()

    def test_every_customer_route_checks_before_it_stores(self):
        for name, before, store in (
                ("order_rx_documents.py", '"checkout_rx"', 'stored_bytes,sha256,pages,typed_json,created_at'),
                ("lens_upload.py", '"lens_rx"', "lens_documents.insert(cursor, customer_id"),
                ("chat_gateway.py", "'chat', info['sha256']", "INSERT INTO chat_attachments\n"),
                ("reship_api.py", '"return_photos"', "return_request.submit(db, cid"),
                ("face_scan.py", "refusal = _screen_upload(file_storage)", "ok, error = store_upload(db, row, file_storage)\n    if not ok:\n        return jsonify({'error': error}), 400\n    return jsonify({'success': True,\n                    'message'"),
                ("chat.py", '"chat_legacy"', "file.save(filepath)")):
            src = self._src(name)
            self.assertLess(src.index(before), src.index(store), name)

    def test_the_notice_is_shown_at_every_upload(self):
        for name in ("templates/checkout.html", "templates/prescription.html", "templates/profile.html",
                     "templates/_lens_eye_cards.html", "templates/face_scan_upload.html",
                     "templates/face_scan_guest.html", "static/js/chat-widget.js"):
            self.assertIn("may be reported to Cyber Crime India", self._src(name), name)

    def test_the_terms_state_the_policy(self):
        terms = self._src("templates/terms-and-conditions.html")
        for text in ('id="prohibited-content"', "cybercrime.gov.in", "Information Technology Act, 2000",
                     "Protection of Children from Sexual Offences Act, 2012"):
            self.assertIn(text, terms)

    def test_deployed(self):
        deploy = self._src("deploy/deploy.py")
        dep_block = deploy[deploy.index("DEPLOY_SET = ("):deploy.index("NEW_IN_RELEASE = (")]
        new_block = deploy[deploy.index("NEW_IN_RELEASE = ("):]
        new_block = new_block[:new_block.index('")\n') + 2]
        self.assertIn('"content_guard.py"', dep_block)
        self.assertIn('"content_guard.py"', new_block)
        self.assertIn("content_guard_module().TABLES", deploy)


if __name__ == "__main__":
    unittest.main()
