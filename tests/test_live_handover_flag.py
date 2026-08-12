"""Tests for the LIVE_HANDOVER_ENABLED gate on POST /api/chat/agent-reply.

Live in-widget human takeover is OFF by default: a validly-signed KET agent
reply must be accepted but NOT delivered (no widget message, no flip to
human_open) unless LIVE_HANDOVER_ENABLED is true. These tests load the real
`chat_gateway` blueprint in isolation (heavy deps stubbed, mirroring
tests/test_support_fallback.py) and exercise the route through a Flask
test client.

    python3 -m unittest tests.test_live_handover_flag
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
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _load_chat_gateway():
    pkg = _stub("flaskr")
    pkg.__path__ = [REPO]
    # acr.py is stdlib-only; load the real module so the blueprint's import works.
    acr_spec = importlib.util.spec_from_file_location("flaskr.acr", os.path.join(REPO, "acr.py"))
    acr_mod = importlib.util.module_from_spec(acr_spec)
    sys.modules["flaskr.acr"] = acr_mod
    acr_spec.loader.exec_module(acr_mod)
    _stub("flaskr.mail", create_ticket_in_db=lambda *a, **k: 1)
    # openai is not installed in the isolated env.
    if "openai" not in sys.modules:
        _stub("openai", OpenAI=lambda *a, **k: None)

    path = os.path.join(REPO, "chat_gateway.py")
    spec = importlib.util.spec_from_file_location("flaskr.chat_gateway", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["flaskr.chat_gateway"] = mod
    spec.loader.exec_module(mod)
    return mod


cg = _load_chat_gateway()

from flask import Flask  # noqa: E402  (import after stubs so flask is real)


def _make_app(live_handover_enabled):
    app = Flask(__name__)
    app.config['LIVE_HANDOVER_ENABLED'] = live_handover_enabled
    app.register_blueprint(cg.bp)
    return app


class LiveHandoverFlagTests(unittest.TestCase):
    def setUp(self):
        # Bypass HMAC so we can focus on the flag; a separate case checks that
        # an invalid signature is still rejected regardless of the flag.
        self._orig_verify = cg._verify_ket_signature
        self._orig_get_db = cg._get_db
        cg._verify_ket_signature = lambda *a, **k: True

    def tearDown(self):
        cg._verify_ket_signature = self._orig_verify
        cg._get_db = self._orig_get_db

    def _post(self, app):
        client = app.test_client()
        return client.post(
            '/api/chat/agent-reply',
            json={'session_id': 's1', 'content': 'hello from agent', 'agent_name': 'Ada'},
            headers={'X-KET-Timestamp': '1', 'X-KET-Signature': 'sig'},
        )

    def test_disabled_accepts_but_ignores_without_touching_db(self):
        # If the gate is bypassed, _get_db would be hit and blow up here.
        def _boom():
            raise AssertionError("DB must not be touched when handover is disabled")
        cg._get_db = _boom

        resp = self._post(_make_app(False))
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.get_json().get('status'), 'ignored')
        self.assertEqual(resp.get_json().get('reason'), 'live_handover_disabled')

    def test_enabled_proceeds_past_the_gate(self):
        # Flag on -> gate lets the request through to session lookup. Use a fake
        # DB that reports "session not found" (404) to prove we got past 202
        # without needing a real MySQL.
        class _Cur:
            def execute(self, *a, **k):
                pass

            def fetchone(self):
                return None

        class _DB:
            def cursor(self):
                return _Cur()

            def close(self):
                pass

        cg._get_db = lambda: _DB()

        resp = self._post(_make_app(True))
        self.assertNotEqual(resp.status_code, 202)
        self.assertEqual(resp.status_code, 404)

    def test_invalid_signature_rejected_regardless_of_flag(self):
        cg._verify_ket_signature = lambda *a, **k: False
        for enabled in (True, False):
            resp = self._post(_make_app(enabled))
            self.assertEqual(resp.status_code, 401)


if __name__ == '__main__':
    unittest.main()
