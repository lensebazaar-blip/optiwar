"""Golden conversation regression tests (Gate-1 item D).

Recorded optical conversations with the classification the platform must produce
for them, driven from ``golden_conversations.json`` so adding a case needs no
code. Run before promoting any AI behaviour change — a new prompt, model,
temperature or tool set can change how replies are phrased, and the phrasing is
what these policy decisions read.

Scope, stated plainly: this locks the *pure text policy* — offer vs promise vs
confirmation, and the navigation-safety gate. It does not exercise the gateway's
wiring, which needs Flask and a database. What it does guarantee about the
gateway is that the wiring still goes through these primitives at all
(``GatewayRoutesThroughPolicyTests``), so the decisions asserted here remain the
decisions production makes.

    python3 -m unittest tests.test_golden_conversations
"""
import importlib.util
import json
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "golden_conversations.json")


def _load_acr():
    spec = importlib.util.spec_from_file_location(
        "acr_under_test_golden", os.path.join(REPO, "acr.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


acr = _load_acr()

with open(FIXTURE, encoding="utf-8") as fh:
    GOLDEN = json.load(fh)["conversations"]


def classify(turn):
    # ``promise`` carries the gateway's guard: a claim of navigation is only an
    # incident when the turn produced no destination. "Opening that for you now"
    # alongside a real target is a description, not an unbacked promise — the
    # reply path checks promises_navigation() only when no navigation happened.
    target = turn.get("target")
    return {
        "offer": acr.offers_navigation(turn["assistant"]),
        "promise": (not target) and acr.promises_navigation(turn["assistant"]),
        "confirm": acr.is_confirmation(turn["customer"]),
        "safe_url": acr.is_safe_nav_url(turn.get("target")),
    }


class GoldenConversationTests(unittest.TestCase):
    def test_every_recorded_turn_classifies_as_recorded(self):
        for convo in GOLDEN:
            for i, turn in enumerate(convo["turns"], 1):
                got = classify(turn)
                for key, want in turn["expect"].items():
                    self.assertEqual(
                        got[key], want,
                        "%s turn %d: %s should be %s but is %s\n  customer: %s"
                        "\n  assistant: %s" % (convo["name"], i, key, want,
                                               got[key], turn["customer"],
                                               turn["assistant"]))

    def test_an_offer_is_never_also_a_promise(self):
        # These are mutually exclusive by design: offering to navigate seeds a
        # pending action, claiming to have navigated is an incident.
        for convo in GOLDEN:
            for turn in convo["turns"]:
                got = classify(turn)
                self.assertFalse(got["offer"] and got["promise"],
                                 "%s: %r is both" % (convo["name"],
                                                     turn["assistant"]))

    def test_the_suite_still_covers_the_failures_it_was_built_for(self):
        # Cheap guard against a future edit that deletes the uncomfortable cases
        # and leaves a green but meaningless suite.
        flat = [t for c in GOLDEN for t in c["turns"]]
        self.assertTrue(any(t["expect"].get("promise") for t in flat),
                        "no promise-without-action conversation left")
        self.assertTrue(any(t["expect"].get("safe_url") is False for t in flat),
                        "no off-site navigation conversation left")
        self.assertTrue(any(t["expect"].get("confirm") for t in flat),
                        "no bare-confirmation conversation left")
        self.assertGreaterEqual(len(GOLDEN), 8)

    def test_every_conversation_says_why_it_exists(self):
        for convo in GOLDEN:
            self.assertTrue(convo.get("why"),
                            "%s has no rationale" % convo["name"])


class GatewayRoutesThroughPolicyTests(unittest.TestCase):
    """The classifications above only describe production if the reply path still
    asks for them. If a rewrite inlines its own notion of 'is this a yes', these
    fixtures keep passing while production changes behaviour — so assert the
    call sites exist."""

    REQUIRED = ("acr.is_safe_nav_url", "acr.offers_navigation",
                "acr.promises_navigation", "acr.is_confirmation",
                "acr.create_pending_action", "acr.get_live_pending_action")

    def setUp(self):
        with open(os.path.join(REPO, "chat_gateway.py"), encoding="utf-8") as fh:
            self.src = fh.read()

    def test_reply_path_calls_each_policy_primitive(self):
        for name in self.REQUIRED:
            self.assertIn(name + "(", self.src,
                          "chat_gateway no longer calls %s" % name)

    def test_navigation_safety_is_checked_before_a_target_is_used(self):
        gate = self.src.index("acr.is_safe_nav_url(")
        seed = self.src.index("acr.create_pending_action(")
        self.assertLess(gate, seed,
                        "an action can be seeded before the safety gate runs")

    def test_no_second_definition_of_confirmation_in_the_gateway(self):
        # One place decides what a confirmation is; a local regex would drift.
        self.assertIsNone(re.search(r"^\s*def .*is_confirm", self.src,
                                    re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
