import importlib.util
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eu_ops"))

from services import frame_offers


def _load_reminders():
    path = ROOT / "eu_ops" / "scripts" / "frame_offer_reminders.py"
    spec = importlib.util.spec_from_file_location("frame_offer_reminders_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FrameOfferOpsTests(unittest.TestCase):
    def test_latest_offer_preserves_settlement_fields(self):
        row = {"offer_uuid": "offer-1", "settled_at": "2026-01-02 03:04:05",
               "settled_ref": "REF-1"}
        with patch.object(frame_offers, "api_call",
                          return_value=(True, {"offers": [row]})):
            ok, offer = frame_offers.latest("930170", 11)
        self.assertTrue(ok)
        self.assertEqual(offer["settled_at"], row["settled_at"])
        self.assertEqual(offer["settled_ref"], row["settled_ref"])

    def test_send_gate_and_approved_copy(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(frame_offers.wa_template(), "")
        with patch.dict(os.environ, {"OW_FRAME_QC_WA_TEMPLATE": "ow_frame_qc_replace"}):
            self.assertEqual(frame_offers.wa_template(), "ow_frame_qc_replace")
            self.assertEqual(frame_offers.send_gate("QC_FAILED"),
                             (True, ""))
            self.assertEqual(frame_offers.send_gate("OUT_OF_STOCK"),
                             (False, "Needs its own approved wording/template"))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                frame_offers.send_gate("QC_FAILED"),
                (False, "Waiting for msg91 approval of ow_frame_qc_replace"))
        with patch.dict(os.environ, {"OW_FRAME_QC_WA_TEMPLATE": "unapproved"}):
            self.assertEqual(frame_offers.wa_template(), "")
            self.assertFalse(frame_offers.send_gate("QC_FAILED")[0])
        self.assertEqual(
            frame_offers.send_gate("QC_FAILED", "unapproved"),
            (False, "Waiting for msg91 approval of ow_frame_qc_replace"))
        order = {"order_id": "930170", "firstname": "Soutrik", "total_minor": 10000}
        failed = {"name": "No Art", "code": "AB28", "price_minor": 10000}
        candidates = [{"product_id": 1, "code": "AB27", "name": "Frame", "size": "52-18",
                       "colour": "Black", "price_minor": 10200}]
        html, text = frame_offers.customer_email(order, failed, candidates, "https://optiwar.com/fq/token")
        self.assertEqual(frame_offers.email_subject("930170"),
                         "Your Optiwar order OW-930170: please choose a replacement frame")
        self.assertIn("There is no extra charge for any of them:", html)
        self.assertIn("No extra charge", html)
        self.assertIn("https://optiwar.com/fq/token", html)
        self.assertIn("Hi Soutrik", text)
        self.assertNotIn("<p>", text)
        self.assertIn("https://optiwar.com/fq/token", text)
        self.assertEqual(frame_offers.WA_BODY,
                         "Hello {{1}}, about your Optiwar order {{2}}: the frame you chose, {{3}}, "
                         "did not pass our quality check, so we won't send it. We have similar "
                         "frames in stock at no extra charge. Please choose a replacement, or ask "
                         "for a full refund, here: {{4}}\n\nThe link works for 7 days.")
        self.assertEqual(frame_offers.wa_values(
            {"order_id": "930170", "firstname": "Soutrik"},
            {"name": "No Art", "code": "AB28"}, "https://optiwar.com/fq/token"),
            ["Soutrik", "OW-930170", "No Art (AB28)", "https://optiwar.com/fq/token"])

    def test_payment_due_email_copy_changes_price_intro(self):
        html, _ = frame_offers.customer_email(
            {"order_id": "1", "firstname": "A"},
            {"name": "Old", "code": "A", "price_minor": 10000},
            [{"code": "B", "name": "New", "size": "", "colour": "",
              "price_minor": 15000}], "https://optiwar.com/fq/x")
        self.assertIn("Prices are shown on each frame:", html)
        self.assertIn("Pay ₹50 more", html)

    def test_day_three_reminder_and_day_seven_expiry(self):
        reminders = _load_reminders()
        now = datetime(2025, 1, 8)
        base = {"offer_uuid": "u", "order_id": "1", "order_line_id": 2,
                "reason": "QC_FAILED", "created_at": now - timedelta(days=3),
                "expires_at": now + timedelta(days=4), "reminder_sent_at": None,
                "failed_price_minor": 10000, "candidates": []}
        client = Mock()
        client.open_offers.return_value = (True, {"offers": [dict(base)]})
        client.reminder_link.return_value = (True, {"link": "https://optiwar.com/fq/x"})
        client.mark.return_value = (True, {})
        notify = Mock()
        notify.send_email.return_value = (True, "ok")
        notify.send_msg91_wa_template.return_value = (True, "ok")
        orders = Mock()
        orders.get_order.return_value = {
            "order_id": "1", "firstname": "A", "email": "a@example.test",
            "telephone": "919999999999", "total": 100,
            "products": [{"order_product_id": 2, "name": "Old", "model": "A"}]}
        with patch.object(frame_offers, "wa_template", return_value="ow_frame_qc_replace"):
            actions = reminders.run(True, now, client, notify, orders)
        self.assertEqual(actions, [{"action": "remind", "offer_uuid": "u"}])
        client.reminder_link.assert_called_once_with("u")
        client.mark.assert_called_once_with("u", "reminder_sent")
        notify.send_email.assert_called_once()
        self.assertTrue(notify.send_email.call_args.args[1].startswith("Reminder: "))
        notify.send_msg91_wa_template.assert_called_once()

        expired = dict(base, created_at=now - timedelta(days=7),
                       expires_at=now - timedelta(seconds=1))
        client.open_offers.return_value = (True, {"offers": [expired]})
        client.mark.reset_mock()
        with patch.object(frame_offers, "wa_template", return_value="ow_frame_qc_replace"):
            actions = reminders.run(True, now, client, notify, orders)
        self.assertEqual(actions, [{"action": "expire", "offer_uuid": "u"}])
        self.assertEqual(client.mark.call_args_list[0].args, ("u", "alerted"))
        self.assertEqual(client.mark.call_args_list[1].args, ("u", "expired"))

    def test_upi_fallback_is_read_only_and_requires_zero_refunds(self):
        call = Mock(return_value=(True, {"items": []}))
        state = frame_offers.upi_fallback_state("not enough balance", "pay_1", call)
        self.assertEqual(state, {"count": 0, "enabled": True})
        call.assert_called_once_with("/payments/pay_1/refunds", method="GET")
        state = frame_offers.upi_fallback_state(
            "not enough balance", "pay_1",
            Mock(return_value=(True, {"items": [{"id": "rf_1"}]})))
        self.assertEqual(state, {"count": 1, "enabled": False})
        self.assertIsNone(frame_offers.upi_fallback_state("other", "pay_1", call))


if __name__ == "__main__":
    unittest.main()
