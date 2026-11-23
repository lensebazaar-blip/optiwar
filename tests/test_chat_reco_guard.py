"""The chat may only offer frames we sell.

On 23 Nov 2026 the assistant listed "JOHN JACOBS JF1563" and "VINCENT CHASE"
at Rs 699 to a customer (ticket OPTIWA-1045): other companies' brands on frames
that do not exist. These tests hold the check that now runs on every reply.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import chat_reco_guard as guard  # noqa: E402

CATALOGUE = [
    {"name": "OPTIWAR", "code": "BB97"},
    {"name": "69", "code": "BP84"},
    {"name": "DOLP", "code": "BH69"},
    {"name": "OPTIWAR Rectangle", "code": "BQ01"},
    {"name": "DIXON TITANIUM TR90 1824", "code": "AG83"},
]

# The reply the customer was sent, word for word.
INVENTED = """Black round frames in medium fit, soutrik dutta:

1. JOHN JACOBS JF1563 — Black, 50-19-140 — ₹699
2. OPTIWAR ROUND — Black, 48-20-140 — ₹699
3. VINCENT CHASE — Black, 50-18-140 — ₹699

All include complimentary prescription lenses. Would you like me to take you there?"""


class InventedFramesTest(unittest.TestCase):
    def test_the_reply_from_the_ticket_loses_every_invented_frame(self):
        reply, removed = guard.check(INVENTED, CATALOGUE, searched=True)
        self.assertEqual(removed, ["JOHN JACOBS JF1563", "OPTIWAR ROUND", "VINCENT CHASE"])
        for word in ("JOHN JACOBS", "VINCENT CHASE", "JF1563", "₹699"):
            self.assertNotIn(word, reply)
        self.assertIn(guard.NOTHING_FOUND, reply)
        self.assertIn("Would you like me to take you there?", reply)

    def test_it_is_caught_even_when_the_turn_did_not_search(self):
        _reply, removed = guard.check(INVENTED, CATALOGUE, searched=False)
        self.assertEqual(len(removed), 3)

    def test_real_frames_stay_and_are_renumbered(self):
        text = ("Here are black frames:\n\n"
                "1. OPTIWAR BB97 — Glossy Black, 52-16-139 — ₹701\n"
                "2. TOMMY BO59 — Black, 52-19-138 — ₹699\n"
                "3. DOLP BH69 — Matte Black, 49-18-138 — ₹699")
        reply, removed = guard.check(text, CATALOGUE, searched=True)
        self.assertEqual(removed, ["TOMMY BO59"])
        self.assertIn("1. OPTIWAR BB97 — Glossy Black", reply)
        self.assertIn("2. DOLP BH69 — Matte Black", reply)
        self.assertNotIn(guard.NOTHING_FOUND, reply)

    def test_a_real_code_under_another_name_gets_the_catalogue_name(self):
        text = "1. **BANFF CENTRE BQ01** — Black, 48-18-138 — ₹699"
        reply, removed = guard.check(text, CATALOGUE, searched=True)
        self.assertEqual(removed, [])
        self.assertEqual(reply, "1. OPTIWAR Rectangle BQ01 — Black, 48-18-138 — ₹699")

    def test_a_brand_name_on_a_real_code_is_still_removed(self):
        text = "1. RAY-BAN BB97 — Black, 52-16-139 — ₹701"
        _reply, removed = guard.check(text, CATALOGUE, searched=True)
        self.assertEqual(removed, ["RAY-BAN BB97"])

    def test_titanium_is_a_material_not_titan(self):
        text = "1. DIXON TITANIUM TR90 1824 AG83 — Brown, 52-15-135 — ₹699"
        self.assertEqual(guard.check(text, CATALOGUE, searched=True), (text, []))


class LeftAloneTest(unittest.TestCase):
    def test_order_lines_are_not_frames(self):
        text = ("You have two orders:\n\n"
                "1. HROSVC-930170 (14 Nov 2026) — payment received, stage Confirmed\n"
                "2. FHVFQM-532870 (10 Nov 2026) — payment received ₹1,299, stage Refunded")
        self.assertEqual(guard.check(text, CATALOGUE, searched=False), (text, []))

    def test_lens_prices_are_not_frames_even_after_a_search(self):
        text = "Lens options:\n- Blue cut lenses — ₹500\n- Progressive — ₹1000 upgrade"
        self.assertEqual(guard.check(text, CATALOGUE, searched=True), (text, []))

    def test_describing_the_customers_own_photo_is_prose(self):
        text = 'I can see "BOSS" on it. What would you like to do — find similar frames?'
        self.assertEqual(guard.check(text, CATALOGUE, searched=True), (text, []))

    def test_ai_glasses_may_be_named_only_while_that_line_is_on(self):
        text = "1. Ray-Ban Meta Wayfarer Gen 2 — Standard 50-22-150 — ₹29,900"
        self.assertEqual(guard.check(text, CATALOGUE, ai_glasses_on=True), (text, []))
        self.assertEqual(guard.check(text, CATALOGUE, ai_glasses_on=False)[1],
                         ["Ray-Ban Meta Wayfarer Gen 2"])

    def test_empty_reply(self):
        self.assertEqual(guard.check("", CATALOGUE), ("", []))


class WiringTest(unittest.TestCase):
    def test_every_chat_reply_goes_through_the_check(self):
        with open(os.path.join(ROOT, "chat_gateway.py"), encoding="utf-8") as fh:
            source = fh.read()
        cleaned = source.index("ai_reply, actions, navigate_url = _clean_ai_reply(ai_reply)")
        checked = source.index("ai_reply = _real_frames_only(ai_reply, session_id, is_india)")
        self.assertLess(cleaned, checked)
        self.assertLess(checked - cleaned, 200)
        self.assertIn("chat_reco_guard.check(", source)

    def test_the_module_ships(self):
        with open(os.path.join(ROOT, "deploy", "deploy.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertEqual(source.count('"chat_reco_guard.py"'), 2)


if __name__ == "__main__":
    unittest.main()
