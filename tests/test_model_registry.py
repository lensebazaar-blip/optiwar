import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_model_registry as reg  # noqa: E402


class RegistryShapeTests(unittest.TestCase):
    def test_every_entry_declares_every_required_field(self):
        for name, entry in reg.REGISTRY.items():
            for field in reg.REQUIRED_FIELDS:
                self.assertIn(field, entry, "%s is missing %s" % (name, field))

    def test_cost_basis_may_be_undeclared_but_must_be_present(self):
        # An absent price is a known gap; an invented one becomes a quoted fact.
        for name, entry in reg.REGISTRY.items():
            self.assertTrue(
                entry["cost_basis"] is reg.UNDECLARED
                or isinstance(entry["cost_basis"], str),
                "%s cost_basis must be UNDECLARED or a sourced string" % name)


class RegistryRuntimeAgreementTests(unittest.TestCase):
    """The point of the registry is that it cannot silently fall behind the
    code, so the agreement is asserted rather than trusted."""

    def test_no_drift_against_ai_client(self):
        d = reg.drift()
        self.assertEqual(d["implemented_not_declared"], [],
                         "a workload was added to ai_client without a registry "
                         "entry: %s" % d["implemented_not_declared"])
        self.assertEqual(d["declared_not_implemented"], [],
                         "the registry declares a workload ai_client no longer "
                         "has: %s" % d["declared_not_implemented"])

    def test_every_workload_resolves_a_provider_and_model(self):
        for name, r in reg.runtime_workloads().items():
            self.assertTrue(r["provider"], "%s has no provider" % name)
            self.assertTrue(r["model"], "%s resolves no model" % name)
            self.assertIsInstance(r["deadline_s"], int)

    def test_drift_is_reported_in_both_directions(self):
        d = reg.drift({"deepseek_chat": {}, "brand_new_workload": {}})
        self.assertEqual(d["implemented_not_declared"], ["brand_new_workload"])
        self.assertIn("openai_vision", d["declared_not_implemented"])


class TimeoutDeclarationTests(unittest.TestCase):
    def test_declared_timeout_env_governs_the_runtime_deadline(self):
        # A registry that names the wrong variable is worse than none: it tells
        # an operator to change a setting that has no effect.
        import ai_client

        for name, entry in reg.REGISTRY.items():
            env = entry["timeout_env"]
            before = ai_client._WORKLOADS[name]["deadline"]()
            os.environ[env] = str(before + 7)
            try:
                self.assertEqual(ai_client._WORKLOADS[name]["deadline"](),
                                 before + 7,
                                 "%s does not honour %s" % (name, env))
            finally:
                del os.environ[env]


if __name__ == "__main__":
    unittest.main()
