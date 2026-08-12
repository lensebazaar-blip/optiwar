"""A failed deployment must restore itself, with nobody watching.

The deploy runs unattended in a 02:00 window. ``cmd_apply`` copies the files and
restarts the service *before* it can know whether the release is healthy, so by
the time a smoke test fails the broken code is already serving; printing the
rollback command and returning is not a recovery. This pins that behaviour, and
pins the other direction too: a preflight or provenance block happens before
anything is copied, so it must not roll back a release that was never applied.

Everything remote is stubbed — no ssh, no production.

    python3 -m unittest tests.test_deploy_apply_rollback
"""
import argparse
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_deploy():
    spec = importlib.util.spec_from_file_location(
        "deploy_apply_under_test", os.path.join(REPO, "deploy", "deploy.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ApplyRollbackTest(unittest.TestCase):
    def setUp(self):
        self.d = _load_deploy()
        self.rolled_back = []
        self.remote_calls = []
        self.d.preflight = lambda: ("main", "abc1234", [])
        self.d.manifest = lambda: ([("acr.py", "oldhash", "newhash", "dead123")],
                                   [], [], [])
        self.d.verify_locally = lambda: (True, ["OK"])
        self.d.worker_health = lambda since: ("active", "0")
        self.d.remote = self._remote
        self.d.subprocess.run = lambda *a, **k: None
        self.rollback_rc = 0
        self.d.cmd_rollback = lambda args: (self.rolled_back.append(args),
                                            self.rollback_rc)[1]

    def _remote(self, cmd, check=True):
        self.remote_calls.append(cmd)
        return "2026-08-12 02:00:00"

    def _args(self):
        return argparse.Namespace(confirm=True)

    def _smoke(self, ok):
        return [("com home", "https://optiwar.com/", "200" if ok else "502", ok)]

    def test_failed_smoke_restores_the_previous_release(self):
        self.d.smoke = lambda: self._smoke(False)
        rc = self.d.cmd_apply(self._args())
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.rolled_back), 1, "broken release left live")

    def test_service_not_active_restores_the_previous_release(self):
        self.d.smoke = lambda: self._smoke(True)
        self.d.worker_health = lambda since: ("failed", "12")
        rc = self.d.cmd_apply(self._args())
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.rolled_back), 1)

    def test_healthy_release_is_left_alone(self):
        self.d.smoke = lambda: self._smoke(True)
        rc = self.d.cmd_apply(self._args())
        self.assertEqual(rc, 0)
        self.assertEqual(self.rolled_back, [])
        self.assertTrue(any("systemctl restart" in c for c in self.remote_calls))
        self.assertEqual(
            sum("systemctl restart" in c for c in self.remote_calls), 1,
            "the window allows exactly one restart")

    def test_failed_rollback_is_reported_as_worse_than_a_failed_deploy(self):
        # The site is still down: a caller that cannot tell this apart from a
        # clean recovery will report a tidy failure and nobody comes.
        self.d.smoke = lambda: self._smoke(False)
        self.rollback_rc = 1
        self.assertEqual(self.d.cmd_apply(self._args()), 2)

    def test_release_escalates_when_the_rollback_did_not_restore_service(self):
        self.d.cmd_migrate = lambda args: 0
        self.d.cmd_apply = lambda args: 2
        self.d.cmd_canary = lambda args: 0
        self.assertEqual(self.d.cmd_release(self._args()), 3)

    def test_release_rolls_back_when_the_canary_blames_the_release(self):
        self.d.cmd_migrate = lambda args: 0
        self.d.cmd_apply = lambda args: 0
        self.d.cmd_canary = lambda args: 1
        self.assertEqual(self.d.cmd_release(self._args()), 1)
        self.assertEqual(len(self.rolled_back), 1)

    def test_release_escalates_when_the_canary_rollback_leaves_it_down(self):
        # Which check condemned the release does not change the verdict: both
        # rollback call sites must report an unrecovered box the same way.
        self.d.cmd_migrate = lambda args: 0
        self.d.cmd_apply = lambda args: 0
        self.d.cmd_canary = lambda args: 1
        self.rollback_rc = 1
        self.assertEqual(self.d.cmd_release(self._args()), 3)

    def test_release_leaves_a_live_release_alone_when_there_is_no_evidence(self):
        # A busy model provider must not cost a healthy release.
        self.d.cmd_migrate = lambda args: 0
        self.d.cmd_apply = lambda args: 0
        self.d.cmd_canary = lambda args: 2
        self.assertEqual(self.d.cmd_release(self._args()), 2)
        self.assertEqual(self.rolled_back, [])

    def test_preflight_block_deploys_nothing_and_does_not_roll_back(self):
        self.d.preflight = lambda: ("side-branch", "abc1234",
                                    ["HEAD is on side-branch"])
        self.d.smoke = lambda: self._smoke(True)
        rc = self.d.cmd_apply(self._args())
        self.assertEqual(rc, 1)
        self.assertEqual(self.rolled_back, [])
        self.assertEqual(self.remote_calls, [], "blocked, yet it touched the box")

    def test_provenance_block_deploys_nothing_and_does_not_roll_back(self):
        self.d.manifest = lambda: ([], ["acr.py: uncommitted edit on the box"],
                                   [], [])
        self.d.smoke = lambda: self._smoke(True)
        rc = self.d.cmd_apply(self._args())
        self.assertEqual(rc, 1)
        self.assertEqual(self.rolled_back, [])
        self.assertEqual(self.remote_calls, [])


if __name__ == "__main__":
    unittest.main()


class DeploySetTest(unittest.TestCase):
    """The scope guard is the reason a whole-tree deploy cannot silently revert a
    live feature, so what is in it and what is not is worth pinning."""

    def setUp(self):
        self.d = _load_deploy()

    def test_the_credential_bearing_divergences_stay_out(self):
        # These five carry credentials hardcoded on the box that exist nowhere in
        # git; deploying the repo's version would blank them into empty strings,
        # silently, because os.environ.get(NAME, "") is a valid string.
        for name in ("pricing.py", "delhivery_union.py", "missing_order_search.py",
                     "dashboard_admin_streamlit.py", "models.py"):
            self.assertNotIn(name, self.d.DEPLOY_SET)

    def test_crm_is_in_scope_for_the_delivery_webhook(self):
        self.assertIn("crm.py", self.d.DEPLOY_SET)

    def test_the_smoke_suite_proves_the_delivery_webhook_is_routed(self):
        # A crm.py that fails to import 404s this route while every page still
        # answers 200 — and MSG91 auto-pauses a webhook that stops returning 2xx.
        urls = [url for _label, url, _want in self.d.SMOKE]
        self.assertIn("https://optiwar.com/support/msg91_delivery_event", urls)
