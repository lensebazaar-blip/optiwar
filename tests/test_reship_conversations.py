"""Returned-parcel regression conversations, driven from
``reship_conversations.json``: for each ledger state the prompt must state the
fact the good reply relies on, the good reply must break no ledger rule, and
the bad reply must be caught with exactly the codes recorded.

    python3 -m unittest tests.test_reship_conversations
"""
import importlib.util
import json
import os
import unittest
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reship_conversations.json")


def _load(name):
    spec = importlib.util.spec_from_file_location(
        "%s_under_test_convo" % name, os.path.join(REPO, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ra = _load("reship_assistant")
acr = _load("acr")

with open(FIXTURE, encoding="utf-8") as fh:
    CASES = json.load(fh)["conversations"]

_DATES = ("paid_at", "returned_at", "abandon_at", "abandoned_at", "reshipped_at")
_EMPTY = {"sub_state": None, "can_pay": False, "fee": 250, "currency": "INR", "paid_at": None,
          "returned_at": None, "abandon_at": None, "days_remaining": None, "holding_days": 60,
          "final_period": False, "on_hold": False, "abandoned_at": None, "reshipped_at": None,
          "original_awb": None, "original_courier": None, "original_track_url": None,
          "new_awb": None, "new_courier": None, "new_track_url": None, "reship_uuid": None}


def model_for(case):
    orders = []
    for o in case["orders"]:
        e = dict(_EMPTY)
        e.update(o)
        for k in _DATES:
            if isinstance(e.get(k), str):
                e[k] = datetime.fromisoformat(e[k])
        orders.append(e)
    return {"orders": orders, "fee": 250, "currency": "INR", "holding_days": 60,
            "support": "support@optiwar.com"}


class ReshipConversationTests(unittest.TestCase):
    def test_the_prompt_states_the_fact_each_good_reply_relies_on(self):
        for case in CASES:
            text = ra.prompt_section(model_for(case))
            for fact in case["facts"]:
                self.assertIn(fact, text, "%s: prompt lacks %r" % (case["name"], fact))

    def test_every_good_reply_keeps_the_ledger_rules(self):
        for case in CASES:
            m = model_for(case)
            self.assertEqual(ra.reply_violations(m, case["good"]), [],
                             "%s: good reply flagged" % case["name"])
            if case.get("good_offers_navigation"):
                self.assertTrue(acr.offers_navigation(case["good"]), case["name"])
            # a good reply never claims a navigation it did not perform
            self.assertFalse(acr.promises_navigation(case["good"]) and
                             "[ACTION:NAVIGATE:" not in case["good"], case["name"])

    def test_every_bad_reply_is_caught_with_the_recorded_codes(self):
        for case in CASES:
            if case.get("bad") is None:
                continue
            got = ra.reply_violations(model_for(case), case["bad"])
            self.assertEqual(sorted(got), sorted(case["bad_codes"]),
                             "%s: %r" % (case["name"], got))

    def test_no_record_means_nothing_is_checked_and_nothing_is_claimed(self):
        empty = model_for({"orders": []})
        self.assertEqual(ra.reply_violations(empty, "You can pay INR 250 now."), [])
        self.assertIn("none on record", ra.prompt_section(empty))

    def test_the_suite_covers_the_ten_questions_it_was_built_for(self):
        names = " ".join(c["name"] for c in CASES).lower()
        for needle in ("returning", "get it again", "paid but not shipped", "cancel",
                       "will my package be abandoned", "day 59", "says abandoned", "new awb (reshipped)",
                       "new awb (not shipped", "no record"):
            self.assertIn(needle, names, needle)
        self.assertGreaterEqual(len(CASES), 10)

    def test_every_case_says_what_it_fixes(self):
        for case in CASES:
            for key in ("name", "customer", "orders", "facts", "good"):
                self.assertIn(key, case, case.get("name"))
            if case.get("bad") is not None:
                self.assertTrue(case.get("bad_codes"), case["name"])


if __name__ == "__main__":
    unittest.main()
