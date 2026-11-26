"""EU Ops confirmation flow for Optiwar customer and face-scan messages."""

import asyncio
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx


ROOT = Path(__file__).resolve().parents[2]
EU_OPS_ROOT = ROOT / "eu_ops"


def _load_main():
    sys.path.insert(0, str(EU_OPS_ROOT))
    package_names = ("config", "services", "auth", "database", "gst_irn")
    saved = {name: sys.modules.get(name) for name in package_names}
    saved.update({name: module for name, module in sys.modules.items()
                  if name.startswith("services.")})
    for name in list(sys.modules):
        if name in package_names or name.startswith("services."):
            sys.modules.pop(name, None)
    gst_irn = types.ModuleType("gst_irn")
    gst_irn.Session = type("Session", (), {})
    sys.modules["gst_irn"] = gst_irn
    spec = importlib.util.spec_from_file_location(
        "ow_customer_routes_main_test", EU_OPS_ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        for name in list(sys.modules):
            if name in package_names or name.startswith("services."):
                sys.modules.pop(name, None)
        sys.modules.update({name: value for name, value in saved.items()
                            if value is not None})
    return module


class OpsCustomerWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _load_main()

    def setUp(self):
        self.user = {
            "id": 17, "username": "ops-user", "full_name": "Ops User",
            "role": "admin",
        }
        self.main.app.dependency_overrides[self.main.get_current_user] = (
            lambda: self.user)
        self.main.reship_svc.ready_to_ship_count = Mock(return_value=0)
        self.main.rpl.actionable_count = Mock(return_value=0)
        self.main.rpl.live_steps = Mock(return_value=[])
        self.main.auth.log_activity = Mock()
        self.calls = []
        self.main.ow_customers_svc.api_call = self.api_call

    def api_call(self, path, method="GET", payload=None):
        self.calls.append((path, method, payload))
        if path.startswith("/ops/api/customers/lookup"):
            return True, {"matches": []}
        if "message-preview?kind=welcome" in path:
            return True, {"subject": "Welcome preview", "text": "Welcome text"}
        if "message-preview?kind=face_scan" in path:
            return True, {
                "subject": "Face scan preview", "text": "Email preview",
                "whatsapp_text": "WhatsApp preview",
            }
        if path == "/ops/api/customers":
            return True, {
                "customer_id": 42,
                "welcome_email": {"ok": True, "error": ""},
            }
        if path == "/ops/api/customers/42/face-scan-request":
            if method == "GET":
                return True, {
                    "customer": {
                        "customer_id": 42, "name": "New Customer",
                        "email": "new@example.com", "phone": "+919876543210",
                    },
                    "scan_request": None, "latest_scan": None,
                }
            return True, {"deliveries": {
                "email": {"ok": True, "error": ""},
                "whatsapp": {"ok": False, "error": "provider"},
            }}
        return False, "unexpected API call"

    def test_customer_template_preserves_intent_and_reads_search_results(self):
        template = (EU_OPS_ROOT / "templates/pages/ow_customers_new.html").read_text()
        self.assertIn('type="hidden" name="intent"', template)
        self.assertIn("event.submitter.value", template)
        self.assertIn("var data = await response.json();", template)
        self.assertIn(
            "var rows = data && Array.isArray(data.results) ? data.results : [];",
            template)

    def post(self, path, data):
        async def send():
            transport = httpx.ASGITransport(app=self.main.app)
            async with httpx.AsyncClient(
                    transport=transport, base_url="http://test") as client:
                return await client.post(path, data=data)
        return asyncio.run(send())

    def test_duplicate_preview_offers_existing_customer_without_create(self):
        def duplicate_lookup(path, method="GET", payload=None):
            self.calls.append((path, method, payload))
            return True, {"matches": [{
                "customer_id": 10, "name": "Existing Customer",
                "email": "existing@example.com", "phone": "+919876543210",
            }]}
        self.main.ow_customers_svc.api_call = duplicate_lookup
        response = self.post("/ow-customers/preview", {
            "name": "New Customer", "email": "existing@example.com",
            "phone": "+919876543210", "intent": "create_and_scan",
            "channels": ["email", "whatsapp"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("Use this customer", response.text)
        self.assertNotIn("Confirm &amp; create", response.text)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(any(method == "POST"
                             for _path, method, _payload in self.calls))

    def test_preview_validation_error_preserves_selected_scan_channel(self):
        def invalid_phone(path, method="GET", payload=None):
            self.calls.append((path, method, payload))
            return False, "invalid phone"
        self.main.ow_customers_svc.api_call = invalid_phone

        response = self.post("/ow-customers/preview", {
            "name": "New Customer", "email": "new@example.com",
            "phone": "bad-phone", "intent": "create_and_scan",
            "channels": ["email"],
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'name="channels" value="email" checked', response.text)
        self.assertIn('name="channels" value="whatsapp"', response.text)
        self.assertNotIn(
            'name="channels" value="whatsapp" checked', response.text)

    def test_confirm_calls_create_then_face_scan(self):
        response = self.post("/ow-customers/confirm", {
            "intent": "create_and_scan", "name": "New Customer",
            "email": "new@example.com", "phone": "+919876543210",
            "channels": ["email", "whatsapp"],
        })
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/ow-customers/42?"))
        self.assertEqual(
            [(method, path) for path, method, _payload in self.calls],
            [
                ("GET", "/ops/api/customers/lookup?email=new%40example.com&phone=%2B919876543210"),
                ("POST", "/ops/api/customers"),
                ("POST", "/ops/api/customers/42/face-scan-request"),
            ])
        self.assertEqual(
            [call.args[1] for call in self.main.auth.log_activity.call_args_list],
            ["created Optiwar customer", "sent Optiwar face-scan request"])

    def test_preview_does_not_send_until_confirm(self):
        response = self.post("/ow-customers/preview", {
            "name": "New Customer", "email": "new@example.com",
            "phone": "+919876543210", "intent": "create_and_scan",
            "channels": ["email", "whatsapp"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("Nothing has been sent", response.text)
        self.assertIn("Welcome text", response.text)
        self.assertIn("Email preview", response.text)
        self.assertIn("WhatsApp preview", response.text)
        self.assertTrue(all(method == "GET" for _path, method, _payload in self.calls))
        self.assertFalse(any("/ops/api/customers" == path
                             for path, _method, _payload in self.calls))

    def test_confirm_face_scan_sends_only_after_preview_step(self):
        self.calls.clear()
        response = self.post("/ow-customers/42/preview", {
            "channels": ["email", "whatsapp"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(method == "GET" for _path, method, _payload in self.calls))
        calls_before_confirm = len(self.calls)
        self.assertGreater(calls_before_confirm, 0)

        response = self.post("/ow-customers/confirm", {
            "intent": "face_scan", "customer_id": "42",
            "channels": ["email", "whatsapp"],
        })
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.calls[-1][1:], (
            "POST", {"channels": ["email", "whatsapp"], "created_by": "Ops User"}))
        self.assertEqual(len(self.calls), calls_before_confirm + 1)


if __name__ == "__main__":
    unittest.main()
