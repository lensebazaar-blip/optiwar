"""error_alerts: every site error reaches admin@optiwar.com, once per window, with details."""
import datetime
import importlib.util
import logging
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AlertsBase(unittest.TestCase):

    def setUp(self):
        from flask import Flask, jsonify
        name = "ea_pkg"
        pkg = types.ModuleType(name); pkg.__path__ = [REPO]; sys.modules[name] = pkg
        self.mails = []
        mail = types.ModuleType(name + ".notice_mail")
        mail.send_content_alert = lambda to, subject, body: self.mails.append((to, subject, body)) or True
        sys.modules[mail.__name__] = mail; pkg.notice_mail = mail
        db = types.ModuleType(name + ".db")

        def down():
            raise RuntimeError("database down")
        db.get_db = down
        sys.modules[db.__name__] = db; pkg.db = db
        for mod in ("dev_defects", "error_alerts"):
            spec = importlib.util.spec_from_file_location("%s.%s" % (name, mod), os.path.join(REPO, mod + ".py"))
            m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; setattr(pkg, mod, m)
            spec.loader.exec_module(m)
        self.ea, self.defects = pkg.error_alerts, pkg.dev_defects
        self.ea._memory.clear()
        real = self.ea._start
        self.addCleanup(setattr, self.ea, "_start", real)

        def inline(app, event):
            self.ea._slots.acquire(blocking=False)
            self.ea.deliver(app, event)
        self.ea._start = inline
        app = self.app = Flask("ea_app")
        app.config.update(TESTING=False, SECRET_KEY="t", PROPAGATE_EXCEPTIONS=False, ERROR_ALERTS_ENABLED=True)
        app.logger.handlers[:] = []
        self.ea.init_error_alerts(app)
        self.addCleanup(app.logger.handlers.clear)

        @app.route("/boom")
        def boom():
            raise ValueError("broke for rahul@example.com on 9876543210 with -3.75")

        @app.route("/soft503")
        def soft():
            return jsonify(ok=False), 503

        @app.route("/defect503")
        def defect():
            self.defects.record("RX_PHOTO_PROVIDER_FAILED", where="rx_photo")
            return jsonify(ok=False), 503

        @app.route("/caught")
        def caught():
            app.logger.error("could not sync the order")
            return "ok"

        @app.route("/flagged")
        def flagged():
            app.logger.error("ACTIVITY:UPLOAD_FLAGGED category=sexual door=chat ip=1.1.1.1")
            return "ok"

        @app.route("/fine")
        def fine():
            return "ok"
        app.add_url_rule("/api/chat/dev-defect", "d", self.defects.browser_defect, methods=["POST"])
        self.client = app.test_client()


class Alerts(AlertsBase):

    def test_an_unhandled_error_mails_the_admins_with_details(self):
        self.assertEqual(self.client.get("/boom").status_code, 500)
        self.assertEqual(len(self.mails), 1)
        to, subject, body = self.mails[0]
        self.assertEqual(to, "admin@optiwar.com")
        self.assertIn("ValueError", subject)
        for fact in ("/boom", "GET", "Traceback", "test_error_alerts.py", "Customer id: guest"):
            self.assertIn(fact, body)

    def test_customer_details_are_masked(self):
        self.client.get("/boom")
        body = self.mails[0][2]
        for private in ("rahul@example.com", "9876543210", "-3.75"):
            self.assertNotIn(private, body)

    def test_a_5xx_without_an_exception_is_reported_once(self):
        self.client.get("/soft503")
        self.assertEqual(len(self.mails), 1)
        self.assertIn("HTTP_503", self.mails[0][1])

    def test_a_defect_that_answers_503_is_not_reported_twice(self):
        self.client.get("/defect503")
        self.assertEqual(len(self.mails), 1)
        self.assertIn("RX_PHOTO_PROVIDER_FAILED", self.mails[0][1])

    def test_an_error_the_code_caught_and_logged_is_reported(self):
        self.client.get("/caught")
        self.assertIn("could not sync the order", self.mails[0][2])

    def test_a_browser_error_arrives_with_where_it_happened(self):
        r = self.client.post("/api/chat/dev-defect", json={
            "code": "SITE_JS_ERROR", "where": "/static/scripts.js:12 x is not a function", "page": "/checkout"})
        self.assertEqual(r.status_code, 204)
        _to, subject, body = self.mails[0]
        self.assertIn("SITE_JS_ERROR", subject)
        self.assertIn("/checkout", subject)
        for fact in ("the customer's browser", "scripts.js:12 x is not a function"):
            self.assertIn(fact, body)

    def test_notices_and_warnings_send_nothing_even_after_log_hygiene(self):
        import logging
        for handler in self.app.logger.handlers:
            handler.setLevel(logging.INFO)
        with self.app.test_request_context("/x"):
            self.app.logger.info("ai-call endpoint=/x outcome=ok")
            self.app.logger.warning("content_guard: Claude Haiku failed (AuthenticationError)")
        self.assertEqual(self.mails, [])
        with self.app.test_request_context("/x"):
            self.app.logger.error("could not sync the order")
        self.assertEqual(len(self.mails), 1)

    def test_business_events_and_healthy_pages_send_nothing(self):
        self.client.get("/flagged")
        self.client.get("/fine")
        self.assertEqual(self.mails, [])

    def test_the_same_error_mails_again_only_after_the_window(self):
        self.client.get("/boom")
        self.client.get("/boom")
        self.assertEqual(len(self.mails), 1)
        for key in list(self.ea._memory):
            self.ea._memory[key] -= datetime.timedelta(minutes=self.ea.WINDOW_MINUTES + 1)
        self.client.get("/boom")
        self.assertEqual(len(self.mails), 2)

    def test_a_failure_while_mailing_does_not_report_itself(self):
        def failing(to, subject, body):
            self.app.logger.error("smtp is down")
            return False
        sys.modules["ea_pkg.notice_mail"].send_content_alert = failing
        self.assertEqual(self.client.get("/boom").status_code, 500)
        self.assertFalse(self.ea._local.delivering)
        self.assertEqual(len(self.ea._memory), 1)

    def test_it_is_silent_in_tests_and_when_switched_off(self):
        self.app.config["ERROR_ALERTS_ENABLED"] = False
        self.client.get("/boom")
        self.app.config.update(ERROR_ALERTS_ENABLED=True, TESTING=True, PROPAGATE_EXCEPTIONS=False)
        self.client.get("/caught")
        self.assertEqual(self.mails, [])


class Wiring(unittest.TestCase):

    def _read(self, *path):
        with open(os.path.join(REPO, *path), encoding="utf-8") as f:
            return f.read()

    def test_every_page_loads_the_browser_reporter(self):
        self.assertIn("js/ow-errors.js", self._read("templates", "base.html"))

    def test_the_app_installs_it(self):
        self.assertIn("init_error_alerts(app)", self._read("__init__.py"))

    def test_it_ships(self):
        deploy = self._read("deploy", "deploy.py")
        self.assertEqual(deploy.count('"error_alerts.py", "static/js/ow-errors.js")'), 2)

    def test_the_reader_reports_a_reply_it_cannot_parse(self):
        self.assertIn("RX_PHOTO_BAD_REPLY", self._read("templates", "prescription.html"))


logging.getLogger("ea_app").propagate = False
