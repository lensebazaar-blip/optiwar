"""A KET lifecycle notice whose phone can never be a WhatsApp number.

OPTIWA-1020/1021/1027 arrived with phone "+91". The send was refused locally
as invalid_phone, retried five times and marked dead. These prove the rule:

  - invalid_phone is permanent: the job is 'skipped' at once, never retried;
  - no email is sent in its place: KET emails resolved/reopened itself (K12),
    and the lifecycle records 'skipped_invalid_phone';
  - a transient failure still retries.

    python3 -m unittest tests.test_ket_invalid_phone
"""
import importlib.util
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

_SAVED = {}
_TOUCHED = ("flask_mail", "flaskr", "flaskr.db", "flaskr.mail", "flaskr.captcha",
            "flaskr.notifications", "flaskr.crm_invalid_phone_under_test")


class _FakeDB:
    def __init__(self, event=None):
        self.event = event
        self.outbox_updates = []
        self.lifecycle_status = []
        self.audits = []

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass


class _FakeCursor:
    def __init__(self, db):
        self.db = db
        self._row = None

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        self._row = None
        if q.startswith("UPDATE whatsapp_delivery_log"):
            self.db.outbox_updates.append((q, params))
        elif q.startswith("SELECT event, ticket_ref, name, email FROM ket_lifecycle_events"):
            self._row = dict(self.db.event) if self.db.event else None
        elif q.startswith("UPDATE ket_lifecycle_events SET processing_status"):
            self.db.lifecycle_status.append(params[0])
        elif q.startswith("INSERT INTO support_event_audit"):
            self.db.audits.append(params)
        elif q.startswith("CREATE TABLE"):
            pass
        else:
            raise AssertionError("unexpected SQL: " + q)
        self.rowcount = 1

    def fetchone(self):
        return self._row

    def close(self):
        pass


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for key, val in attrs.items():
        setattr(mod, key, val)
    sys.modules[name] = mod
    return mod


crm = None
DB = {"db": None}
SENT = []
SEND_OK = {"ok": True}


def _send_email(to, subject, body_html, body_text=None, cc_emails=None):
    SENT.append((to, subject, body_html))
    return SEND_OK["ok"]


def setUpModule():
    global crm
    for name in _TOUCHED:
        _SAVED[name] = sys.modules.get(name)
    if "flask_mail" not in sys.modules:
        _stub("flask_mail", Message=object)
    import notifications
    pkg = types.ModuleType("flaskr")
    pkg.__path__ = [REPO]
    sys.modules["flaskr"] = pkg
    _stub("flaskr.db", get_db=lambda *a, **k: DB["db"])
    _stub("flaskr.mail", send_contact_email=lambda *a, **k: None,
          create_ticket_in_db=lambda *a, **k: None)
    _stub("flaskr.captcha", CaptchaGenerator=object)
    _stub("flaskr.notifications", send_email=_send_email,
          support_lifecycle_email=notifications.support_lifecycle_email)
    spec = importlib.util.spec_from_file_location(
        "flaskr.crm_invalid_phone_under_test", os.path.join(REPO, "crm.py"))
    crm = importlib.util.module_from_spec(spec)
    sys.modules["flaskr.crm_invalid_phone_under_test"] = crm
    spec.loader.exec_module(crm)
    crm._KET_SCHEMA_READY = True


def tearDownModule():
    for name, mod in _SAVED.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


INVALID = {"ok": False, "request_id": "", "status": "skipped", "error": "invalid_phone"}
EVENT = {"event": "resolved", "ticket_ref": "OPTIWA-1020", "name": "Asha",
         "email": "asha@example.com"}


class InvalidPhoneTests(unittest.TestCase):
    def setUp(self):
        SENT.clear()
        SEND_OK["ok"] = True

    def _finalize(self, result, attempt=1, event=EVENT):
        DB["db"] = _FakeDB(event=event)
        crm._finalize_whatsapp_job("ket:e1:whatsapp", "e1", result, attempt, "optiwar.in")
        return DB["db"]

    def test_invalid_phone_is_skipped_at_once_and_not_emailed(self):
        db = self._finalize(INVALID)
        self.assertEqual(len(db.outbox_updates), 1)
        sql, params = db.outbox_updates[0]
        self.assertIn("status='skipped'", sql)
        self.assertNotIn("next_attempt_at", sql)
        self.assertEqual(params[0], "invalid_phone")
        self.assertEqual(SENT, [])
        self.assertEqual(db.lifecycle_status, ["skipped_invalid_phone"])
        kinds = [a[0] for a in db.audits]
        self.assertEqual(kinds, ["whatsapp_result"])
        self.assertEqual(db.audits[0][7], "skipped")

    def test_skipped_job_is_never_claimed_again(self):
        with open(os.path.join(REPO, "crm.py")) as fh:
            src = fh.read()
        for fn, nxt in (("def _claim_due_whatsapp_job", "def _finalize_whatsapp_job"),
                        ("def _scan_whatsapp_outbox", "def start_whatsapp_outbox_worker")):
            body = src[src.index(fn):src.index(nxt)]
            self.assertIn("status='pending' OR status='failed'", body)
            self.assertNotIn("skipped", body)

    def test_reopened_with_invalid_phone_is_not_emailed_either(self):
        db = self._finalize(INVALID, event=dict(EVENT, event="reopened"))
        self.assertEqual(SENT, [])
        self.assertEqual(db.lifecycle_status, ["skipped_invalid_phone"])

    def test_no_email_fallback_remains(self):
        self.assertFalse(hasattr(crm, "_email_lifecycle_fallback"))

    def test_invalid_phone_on_last_attempt_is_still_skipped_not_dead(self):
        db = self._finalize(INVALID, attempt=crm.WA_MAX_ATTEMPTS)
        self.assertIn("status='skipped'", db.outbox_updates[0][0])
        self.assertEqual(db.lifecycle_status, ["skipped_invalid_phone"])

    def test_transient_failure_retries_and_sends_no_email(self):
        db = self._finalize({"ok": False, "request_id": "", "status": "failed",
                             "error": "http_500"})
        sql, _ = db.outbox_updates[0]
        self.assertIn("status='failed'", sql)
        self.assertIn("next_attempt_at", sql)
        self.assertEqual(SENT, [])
        self.assertEqual(db.lifecycle_status, [])

    def test_success_is_unchanged(self):
        db = self._finalize({"ok": True, "request_id": "r1", "status": "sent", "error": ""})
        self.assertIn("status='sent'", db.outbox_updates[0][0])
        self.assertEqual(db.lifecycle_status, ["notified"])
        self.assertEqual(SENT, [])


if __name__ == "__main__":
    unittest.main()
