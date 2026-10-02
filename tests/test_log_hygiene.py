"""B5: production logs at INFO, and contact details / credentials are masked."""
import importlib.util
import io
import logging
import os
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lh = _load("log_hygiene_under_test", "log_hygiene.py")


def _src(rel):
    with open(os.path.join(REPO, rel)) as fh:
        return fh.read()


class RedactTest(unittest.TestCase):
    def test_contact_details_and_credentials_are_masked(self):
        cases = {
            "Sent to customer navneet.singh@example.com for T1":
                "Sent to customer <email> for T1",
            "phone 63933-43918 ok": "phone <phone> ok",
            "to=+91 9876543210": "to=<phone>",
            "to=919876543210": "to=<phone>",
            "to 9876543210.": "to <phone>.",
            "Authorization: Bearer sk-abc.DEF_123": "Authorization: Bearer <redacted>",
            "GET /x?token=abc123&page=2": "GET /x?token=<redacted>&page=2",
            "authkey api_key=XYZ, next": "authkey api_key=<redacted>, next",
            "link https://optiwar.in/f/AbCdEfGhIjKlMnOpQrSt":
                "link https://optiwar.in/f/<redacted>",
        }
        for raw, want in cases.items():
            self.assertEqual(lh.redact(raw), want, raw)

    def test_operational_identifiers_are_kept(self):
        for safe in (
            "order OW-BSNICP-523998 awb 12386210139403 forward 7X118669628",
            "pickup 004f021e-b112-4232-a64e-67a333899a46 status BOOKED",
            "payment:pay_bc272e26348142 order:833007-RZP reason:AMOUNT_MISMATCH",
            "event_id=evt_123 ticket_ref=OPTIWA-1032 rid=r2 has_phone=True",
            "provider=deepseek model=deepseek-v4-flash latency_ms=842 at 1790915734",
            "IP:203.0.113.7 Path:/checkout user_id=4512 keys=['user_id']",
        ):
            self.assertEqual(lh.redact(safe), safe)


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.buf = io.StringIO()
        self.handler = logging.StreamHandler(self.buf)
        self.handler.addFilter(lh.RedactingFilter())
        self.log = logging.getLogger("log_hygiene_test.filter")
        self.log.propagate = False
        self.log.addHandler(self.handler)
        self.log.setLevel(logging.INFO)

    def tearDown(self):
        self.log.removeHandler(self.handler)

    def test_args_and_exception_text_are_masked(self):
        self.log.info("login %s from %s", "a@b.co", "9876543210")
        try:
            raise ValueError("bad address x@y.in")
        except ValueError:
            self.log.exception("failed for order OW-ABC-1")
        out = self.buf.getvalue()
        self.assertIn("login <email> from <phone>", out)
        self.assertIn("bad address <email>", out)
        self.assertIn("OW-ABC-1", out)
        self.assertNotIn("a@b.co", out)
        self.assertNotIn("x@y.in", out)


class LevelTest(unittest.TestCase):
    def test_default_is_info_and_env_is_honoured(self):
        self.assertEqual(lh.configured_level(""), logging.INFO)
        self.assertEqual(lh.configured_level("debug"), logging.DEBUG)
        self.assertEqual(lh.configured_level("WARNING"), logging.WARNING)
        self.assertEqual(lh.configured_level("bogus"), logging.INFO)

    def _install(self, env):
        root = logging.getLogger()
        saved = (root.level, list(root.handlers),
                 {n: logging.getLogger(n).level for n in lh.QUIET_LIBRARIES})
        self.addCleanup(self._restore, saved)
        root_handler = logging.StreamHandler(io.StringIO())
        root_handler.setLevel(logging.DEBUG)
        root.addHandler(root_handler)
        root.setLevel(logging.DEBUG)  # what basicConfig(level=DEBUG) leaves
        app_handler = logging.StreamHandler(io.StringIO())
        app_handler.setLevel(logging.DEBUG)
        app = mock.Mock()
        app.logger = logging.getLogger("log_hygiene_test.app")
        app.logger.handlers = [app_handler]
        app.logger.setLevel(logging.DEBUG)
        with mock.patch.dict(os.environ, env, clear=False):
            if "LOG_LEVEL" not in env:
                os.environ.pop("LOG_LEVEL", None)
            lh.install(app)
            lh.install(app)
        return app, app_handler, root_handler

    def _restore(self, saved):
        root = logging.getLogger()
        level, handlers, quiet = saved
        root.setLevel(level)
        root.handlers = handlers
        for n, lvl in quiet.items():
            logging.getLogger(n).setLevel(lvl)

    def test_install_lifts_debug_to_info_everywhere(self):
        app, app_handler, root_handler = self._install({})
        self.assertEqual(app.logger.level, logging.INFO)
        self.assertEqual(logging.getLogger().level, logging.INFO)
        for h in (app_handler, root_handler):
            self.assertEqual(h.level, logging.INFO)
            self.assertEqual(
                sum(isinstance(f, lh.RedactingFilter) for f in h.filters), 1)
        self.assertFalse(logging.getLogger("openai._base_client")
                         .isEnabledFor(logging.INFO))

    def test_ai_http_clients_stay_quiet_even_at_debug(self):
        self._install({"LOG_LEVEL": "DEBUG"})
        self.assertEqual(logging.getLogger().level, logging.DEBUG)
        for name in ("openai._base_client", "httpcore.http11", "httpx"):
            self.assertFalse(logging.getLogger(name).isEnabledFor(logging.DEBUG))


class WiringTest(unittest.TestCase):
    def test_app_factory_installs_hygiene_and_sets_no_debug_level(self):
        src = _src("__init__.py")
        self.assertIn("log_hygiene.install(app)", src)
        self.assertNotIn("logging.DEBUG", src)
        self.assertLess(src.index("register_blueprint(chat_gateway_bp)"),
                        src.index("log_hygiene.install(app)"))

    def test_face_scan_blueprint_is_registered_only_behind_its_flag(self):
        src = _src("__init__.py")
        gate = src.index("FACE_SCAN_LINK_ENABLED")
        self.assertLess(gate, src.index("from . import face_scan"))

    def test_no_module_forces_debug_on_the_root_logger(self):
        self.assertNotIn("basicConfig", _src("orders.py"))

    def test_checkout_prefill_prints_no_contact_details(self):
        prefill = [l for l in _src("models.py").splitlines() if "[PREFILL]" in l]
        self.assertTrue(prefill)
        for line in prefill:
            for leaked in ("email", "phone", "address')", "{prefill}"):
                self.assertNotIn(leaked, line, line)

    def test_deploy_ships_the_factory_and_the_module(self):
        d = _load("deploy_under_log_test", "deploy/deploy.py")
        self.assertIn("__init__.py", d.DEPLOY_SET)
        self.assertIn("log_hygiene.py", d.DEPLOY_SET)
        self.assertIn("log_hygiene.py", d.NEW_IN_RELEASE)
        self.assertIn("__init__.py", d.REVIEWED_DRIFT)


if __name__ == "__main__":
    unittest.main()
