"""Tests for the AI-first support Layer-2 fallback + health detection (Phase A).

Covers the provider-agnostic pieces of crm.py that can run without the full
Flask app or a live database:

  - the customer-facing fallback message never says "AI unavailable";
  - TCP/AI health probes fail safe (return (False, reason), never raise);
  - AI health reports "no_api_key" when the provider isn't configured;
  - KET reachability derives host/port from the configured URL.

External deps not present in the test env are stubbed so crm imports in
isolation, matching tests/test_ai_wrapper.py.

    python3 -m unittest tests.test_support_fallback
"""
import importlib.util
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _stub(name, **attrs):
    m = sys.modules.get(name)
    if m is None:
        m = types.ModuleType(name)
        sys.modules[name] = m
    # Always (re)apply the attrs we depend on, even if another test already
    # registered a partial stub for this module.
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _load_crm():
    # Stub the flaskr package + the submodules crm imports at module load.
    pkg = _stub("flaskr")
    pkg.__path__ = [REPO]
    _stub("flaskr.mail", send_contact_email=lambda *a, **k: True,
          create_ticket_in_db=lambda *a, **k: 1)
    _stub("flaskr.captcha", CaptchaGenerator=lambda *a, **k: object())
    _stub("flaskr.db", get_db=lambda: None)
    # requests / flask_mail may not be installed in the isolated env.
    if "requests" not in sys.modules:
        req = _stub("requests")
        req.auth = _stub("requests.auth", HTTPBasicAuth=lambda *a, **k: None)
    _stub("flask_mail", Message=object)

    path = os.path.join(REPO, "crm.py")
    spec = importlib.util.spec_from_file_location("flaskr.crm", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["flaskr.crm"] = mod
    spec.loader.exec_module(mod)
    return mod


class SupportFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.crm = _load_crm()

    def test_fallback_message_is_reassuring(self):
        msg = self.crm.AI_FALLBACK_MESSAGE
        self.assertIn("high demand", msg.lower())
        # Must never expose a raw failure to the customer.
        self.assertNotIn("unavailable", msg.lower())
        self.assertNotIn("error", msg.lower())

    def test_tcp_health_unconfigured_host(self):
        ok, detail = self.crm._check_tcp_health("", 0)
        self.assertFalse(ok)
        self.assertEqual(detail, "not_configured")

    def test_tcp_health_unreachable_is_safe(self):
        # Reserved TEST-NET address: should fail fast, never raise.
        ok, detail = self.crm._check_tcp_health("192.0.2.1", 9, timeout=1.0)
        self.assertFalse(ok)
        self.assertIsInstance(detail, str)

    def test_ai_health_without_key(self):
        prev = os.environ.pop("OPENAI_API_KEY", None)
        try:
            ok, detail = self.crm._check_ai_health()
            self.assertFalse(ok)
            self.assertEqual(detail, "no_api_key")
        finally:
            if prev is not None:
                os.environ["OPENAI_API_KEY"] = prev

    def test_ket_health_returns_tuple(self):
        ok, detail = self.crm._check_ket_health()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(detail, str)


class KetTicketEventAuthTests(unittest.TestCase):
    """HMAC verification for the inbound KET ticket-lifecycle webhook."""

    @classmethod
    def setUpClass(cls):
        cls.crm = _load_crm()
        import hashlib as _h
        import hmac as _hm
        cls._h, cls._hm = _h, _hm
        from flask import Flask
        cls.app = Flask(__name__)
        cls.app.config["OPTIWAR_WEBHOOK_SECRET"] = "test-secret"

    def _sign(self, ts, body, secret="test-secret"):
        return self._hm.new(secret.encode(), f"{ts}:{body}".encode(), self._h.sha256).hexdigest()

    def test_valid_signature_accepted(self):
        import time as _t
        with self.app.app_context():
            ts = str(int(_t.time()))
            body = '{"event":"resolved","ticket_ref":"42"}'
            self.assertTrue(self.crm._verify_ket_signature(body, ts, self._sign(ts, body)))

    def test_wrong_signature_rejected(self):
        import time as _t
        with self.app.app_context():
            ts = str(int(_t.time()))
            body = '{"event":"resolved"}'
            self.assertFalse(self.crm._verify_ket_signature(body, ts, "deadbeef"))

    def test_stale_timestamp_rejected(self):
        with self.app.app_context():
            ts = "1000000000"  # far in the past
            body = "{}"
            self.assertFalse(self.crm._verify_ket_signature(body, ts, self._sign(ts, body)))

    def test_missing_secret_fails_closed(self):
        import time as _t
        from flask import Flask
        app = Flask(__name__)
        app.config["OPTIWAR_WEBHOOK_SECRET"] = ""
        with app.app_context():
            ts = str(int(_t.time()))
            self.assertFalse(self.crm._verify_ket_signature("{}", ts, self._sign(ts, "{}")))

    def test_event_template_map(self):
        # Only resolved + reopened are actioned; both map to their MSG91 template.
        self.assertEqual(set(self.crm._KET_EVENT_TEMPLATES), {"resolved", "reopened"})
        self.assertEqual(self.crm._KET_EVENT_TEMPLATES["resolved"], "support_ticket_resolved")
        self.assertEqual(self.crm._KET_EVENT_TEMPLATES["reopened"], "support_ticket_reopened")

    def test_schema_version(self):
        self.assertEqual(self.crm.KET_SCHEMA_VERSION, 1)


if __name__ == "__main__":
    unittest.main()
