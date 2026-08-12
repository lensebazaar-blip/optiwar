import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_change_register as reg  # noqa: E402

MANIFEST = """release 20260812-071202
repo main @ dc9c162
acr.py 41e9e6aa19fd -> 0584763b859f (was 6d701a111)
ai_client.py 8c5d9dc25b80 -> 729f21691565 (was 3cecdc0e2)
chat_gateway.py 35994c8bcfac -> 9c05b325ad10 (was 7a381c293)
"""


class ClassificationTests(unittest.TestCase):
    def test_a_commit_touching_no_ai_path_is_not_an_ai_change(self):
        # However it is worded. Otherwise the register fills with copy edits
        # that happen to mention the assistant.
        self.assertEqual(
            reg.classify(["templates/home.html"],
                         "Reword the AI assistant intro prompt copy"), set())

    def test_paths_decide_membership_and_the_subject_refines_the_class(self):
        self.assertEqual(reg.classify(["ai_client.py"], "bump deepseek model"),
                         {"model/provider"})
        self.assertEqual(
            reg.classify(["ai_client.py"], "raise per-workload slots and deadline"),
            {"model/provider", "capacity"})

    def test_capacity_and_model_changes_do_not_read_the_same(self):
        # Different blast radius; an audit that merges them is useless.
        cap = reg.classify(["ai_client.py"], "shed load at 2 slots per worker")
        mod = reg.classify(["ai_client.py"], "switch chat to deepseek-v4-flash")
        self.assertIn("capacity", cap)
        self.assertNotIn("capacity", mod)

    def test_policy_and_closure_paths(self):
        self.assertIn("policy", reg.classify(["acr.py"], "canary gate"))
        self.assertIn("closure",
                      reg.classify(["acr_closure_job.py"], "sweep outcomes"))


class ManifestTests(unittest.TestCase):
    def test_parses_release_commit_and_files(self):
        m = reg.parse_manifest(MANIFEST)
        self.assertEqual(m["release"], "20260812-071202")
        self.assertEqual(m["commit"], "dc9c162")
        self.assertEqual(sorted(m["files"]),
                         ["acr.py", "ai_client.py", "chat_gateway.py"])
        self.assertEqual(m["files"]["acr.py"], ("41e9e6aa19fd", "0584763b859f"))

    def test_a_manifest_that_cannot_say_what_it_deployed_is_not_evidence(self):
        self.assertIsNone(reg.parse_manifest("release 20260812-071202\n"))
        self.assertIsNone(reg.parse_manifest("repo main @ dc9c162\n"))
        self.assertIsNone(reg.parse_manifest(""))


class DeployedInTests(unittest.TestCase):
    MANIFESTS = [{"release": "20260810-000000", "commit": "aaa", "files": {}},
                 {"release": "20260812-071202", "commit": "bbb", "files": {}}]

    def test_reports_the_earliest_release_that_contains_the_commit(self):
        # Contained by the later release only.
        rel = reg.deployed_in("x", self.MANIFESTS,
                              lambda a, b: b == "bbb")
        self.assertEqual(rel, "20260812-071202")

    def test_contained_by_both_reports_the_first(self):
        rel = reg.deployed_in("x", self.MANIFESTS, lambda a, b: True)
        self.assertEqual(rel, "20260810-000000")

    def test_undeployed_commit_reports_none_rather_than_guessing(self):
        self.assertIsNone(reg.deployed_in("x", self.MANIFESTS,
                                          lambda a, b: False))

    def test_unknown_ancestry_is_not_deployed(self):
        def boom(a, b):
            raise RuntimeError("shallow clone")

        self.assertIsNone(reg.deployed_in("x", self.MANIFESTS, boom))

    def test_no_manifests_means_nothing_is_known_to_be_deployed(self):
        self.assertIsNone(reg.deployed_in("x", [], lambda a, b: True))


class AgainstThisRepoTests(unittest.TestCase):
    def test_register_finds_this_repos_ai_history_and_stays_selective(self):
        entries = reg.build(since="2026-01-01", manifests_dir=None)
        self.assertTrue(entries, "no AI commits found in this repository")
        for e in entries:
            self.assertTrue(e["paths"], "%s has no AI path" % e["commit"])
            self.assertTrue(e["classes"])
            self.assertIsNone(e["release"])  # no manifests supplied


if __name__ == "__main__":
    unittest.main()
