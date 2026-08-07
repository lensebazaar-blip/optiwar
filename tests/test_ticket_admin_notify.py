"""Regression tests for the internal admin ticket notification (Item 9).

Verifies the contract that the admin notification is best-effort and strictly
out-of-transaction:

  - when APP_TICKET_EMAIL_ENABLED is false, no SMTP is attempted and the
    function returns False (KET remains the customer-facing sender);
  - when enabled and SMTP succeeds, exactly one message is sent to the
    configured admin recipient with the distinguishing subject prefix;
  - a transient SMTP failure is retried and eventually succeeds;
  - a persistent SMTP failure NEVER raises (so ticket creation / customer
    confirmation cannot be blocked) and returns False after the retry budget.

Runs without pytest and without the full Flask app: `flask` and `flaskr.db`
are stubbed so the real mail module imports in isolation.

    python3 -m unittest tests.test_ticket_admin_notify
"""
import importlib.util
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeApp:
    def __init__(self, config):
        self.config = config
        self.logger = _FakeLogger()


def _install_flask_stub(config):
    flask_mod = types.ModuleType("flask")
    flask_mod.current_app = _FakeApp(config)
    request = types.SimpleNamespace(remote_addr="127.0.0.1")
    flask_mod.request = request
    sys.modules["flask"] = flask_mod
    return flask_mod


def _load_mail():
    # fake `flaskr` package + `flaskr.db` so the relative import resolves
    pkg = types.ModuleType("flaskr")
    pkg.__path__ = [REPO]
    sys.modules["flaskr"] = pkg
    db = types.ModuleType("flaskr.db")
    db.get_db = lambda *a, **k: None
    sys.modules["flaskr.db"] = db

    spec = importlib.util.spec_from_file_location(
        "flaskr.mail", os.path.join(REPO, "mail.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["flaskr.mail"] = mod
    spec.loader.exec_module(mod)
    return mod


BASE_CONFIG = {
    "MAIL_SERVER": "mail.example.test",
    "MAIL_PORT": 587,
    "MAIL_USERNAME": "admin@optiwar.com",
    "MAIL_PASSWORD": "x",
    "ADMIN_NOTIFY_EMAIL": "admin@optiwar.com",
}


class _FakeSMTP:
    """Records sent messages. `fail_times` first attempts raise, rest succeed."""

    sent = []
    attempts = 0
    fail_times = 0

    def __init__(self, server, port, timeout=None):
        type(self).attempts += 1
        self._should_fail = type(self).attempts <= type(self).fail_times

    def __enter__(self):
        if self._should_fail:
            raise OSError("simulated SMTP connect failure")
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg):
        type(self).sent.append(msg)

    @classmethod
    def reset(cls, fail_times=0):
        cls.sent = []
        cls.attempts = 0
        cls.fail_times = fail_times


class TicketAdminNotifyTests(unittest.TestCase):
    def setUp(self):
        # fresh module + flask stub per test so config changes are isolated
        for name in ("flask", "flaskr", "flaskr.db", "flaskr.mail"):
            sys.modules.pop(name, None)

    def _prep(self, enabled, fail_times=0):
        config = dict(BASE_CONFIG)
        config["APP_TICKET_EMAIL_ENABLED"] = enabled
        _install_flask_stub(config)
        mail = _load_mail()
        _FakeSMTP.reset(fail_times=fail_times)
        mail.smtplib.SMTP = _FakeSMTP
        mail.time.sleep = lambda *a, **k: None  # keep retries instant
        return mail

    def test_disabled_flag_skips_smtp(self):
        mail = self._prep(enabled=False)
        result = mail.send_contact_email(
            name="A", email="a@x.com", phone="1", subject="Sub",
            message="Msg", ticket_id=101,
        )
        self.assertFalse(result)
        self.assertEqual(_FakeSMTP.attempts, 0)
        self.assertEqual(_FakeSMTP.sent, [])

    def test_enabled_success_sends_to_admin(self):
        mail = self._prep(enabled=True)
        result = mail.send_contact_email(
            name="A", email="cust@x.com", phone="1", subject="Broken frame",
            message="Msg", ticket_id=202,
        )
        self.assertTrue(result)
        self.assertEqual(len(_FakeSMTP.sent), 1)
        msg = _FakeSMTP.sent[0]
        self.assertEqual(msg["To"], "admin@optiwar.com")
        self.assertIn("[Optiwar App]", msg["Subject"])
        self.assertIn("202", msg["Subject"])
        self.assertEqual(msg["Reply-To"], "cust@x.com")

    def test_transient_failure_then_success(self):
        mail = self._prep(enabled=True, fail_times=2)
        result = mail.send_contact_email(
            name="A", email="a@x.com", phone="1", subject="S",
            message="M", ticket_id=303,
        )
        self.assertTrue(result)
        self.assertEqual(_FakeSMTP.attempts, 3)
        self.assertEqual(len(_FakeSMTP.sent), 1)

    def test_persistent_failure_never_raises(self):
        mail = self._prep(enabled=True, fail_times=99)
        try:
            result = mail.send_contact_email(
                name="A", email="a@x.com", phone="1", subject="S",
                message="M", ticket_id=404,
            )
        except Exception as e:  # noqa: BLE001 - contract is "never raises"
            self.fail(f"send_contact_email must not raise, but raised: {e!r}")
        self.assertFalse(result)
        self.assertEqual(_FakeSMTP.attempts, 3)  # bounded retry budget
        self.assertEqual(_FakeSMTP.sent, [])


if __name__ == "__main__":
    unittest.main()
