import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "flask_mail" not in sys.modules:
    try:
        import flask_mail  # noqa: F401
    except ImportError:
        _fm = types.ModuleType("flask_mail")
        _fm.Message = type("Message", (), {})
        sys.modules["flask_mail"] = _fm

import notifications  # noqa: E402


class WhatsAppRecipientTests(unittest.TestCase):
    """MSG91's 1-29 Sep failure log: 'phone number is malformed' (a bare '91')
    and 'blocked prefixes (60)' (a 10-digit Indian mobile sent without 91)."""

    def test_ten_digit_indian_mobile_gets_country_code(self):
        self.assertEqual(notifications.whatsapp_recipient("6012345678"), "916012345678")
        self.assertEqual(notifications.whatsapp_recipient("98765 43210"), "919876543210")
        self.assertEqual(notifications.whatsapp_recipient("09876543210"), "919876543210")

    def test_e164_and_international_forms_pass_through_as_digits(self):
        self.assertEqual(notifications.whatsapp_recipient("+91 98765-43210"), "919876543210")
        self.assertEqual(notifications.whatsapp_recipient("0049 1512 3456789"), "4915123456789")
        self.assertEqual(notifications.whatsapp_recipient("+4915123456789"), "4915123456789")

    def test_bare_country_code_or_garbage_is_refused(self):
        for bad in ("91", "+91", "", None, "abc", "1234567", "1" * 16):
            self.assertEqual(notifications.whatsapp_recipient(bad), "", bad)

    def test_send_skips_invalid_recipient_without_calling_msg91(self):
        app = mock.MagicMock()
        app.config = {"MSG91_AUTH_KEY": "k", "MSG91_WHATSAPP_NUMBER": "n"}
        with mock.patch.object(notifications, "current_app", app), \
                mock.patch.object(notifications.http_requests, "post") as post:
            out = notifications.send_whatsapp_tracked("+91", "support_ticket_resolved", {})
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["error"], "invalid_phone")
        post.assert_not_called()

    def test_send_posts_the_normalised_recipient(self):
        app = mock.MagicMock()
        app.config = {"MSG91_AUTH_KEY": "k", "MSG91_WHATSAPP_NUMBER": "n"}
        resp = mock.MagicMock(status_code=200)
        resp.json.return_value = {"request_id": "r1"}
        with mock.patch.object(notifications, "current_app", app), \
                mock.patch.object(notifications.http_requests, "post", return_value=resp) as post:
            out = notifications.send_whatsapp_tracked("6012345678", "return_started", {})
        self.assertTrue(out["ok"])
        sent = post.call_args.kwargs["json"]["payload"]["template"]["to_and_components"][0]["to"]
        self.assertEqual(sent, ["916012345678"])


if __name__ == "__main__":
    unittest.main()
