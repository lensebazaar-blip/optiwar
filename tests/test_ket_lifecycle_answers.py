"""KET's 2 Oct integration answers, as the lifecycle receiver applies them.

  - K9: ``ticket_uid`` is read first; ``ticket_id`` (same UUID) is the fallback.
  - K11: KET sends ``reopened`` when an agent first opens a new ticket. A
    reopened with no earlier resolved for that ticket is that acceptance: the
    session is not touched and the customer gets no "reopened" WhatsApp.
  - A real reopen (after a resolved) still enqueues the WhatsApp.

    python3 -m unittest tests.test_ket_lifecycle_answers
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_support_fallback import _load_crm  # noqa: E402

_TOUCHED = ("flaskr", "flaskr.db", "flaskr.mail", "flaskr.captcha", "flaskr.crm",
            "flask_mail", "requests", "requests.auth")
_SAVED = {}


def setUpModule():
    for name in _TOUCHED:
        _SAVED[name] = sys.modules.get(name)
    db = sys.modules.get("flaskr.db")
    _SAVED["get_db"] = vars(db).get("get_db") if db else None


def tearDownModule():
    db = sys.modules.get("flaskr.db")
    if db is not None and _SAVED.get("get_db") is not None:
        db.get_db = _SAVED["get_db"]
    for name in _TOUCHED:
        if _SAVED[name] is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = _SAVED[name]


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def close(self):
        pass


class _DB:
    def __init__(self, rows=None, fail=False):
        self.cur = _Cursor(list(rows or []))
        self.fail = fail

    def cursor(self):
        if self.fail:
            raise RuntimeError("db down")
        return self.cur


class WasResolvedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.crm = _load_crm()
        from flask import Flask
        cls.app = Flask(__name__)

    def _run(self, db, *args):
        sys.modules["flaskr.db"].get_db = lambda: db
        with self.app.app_context():
            return self.crm._was_resolved(*args)

    def test_an_earlier_resolved_for_the_ticket_is_found(self):
        db = _DB(rows=[(1,)])
        self.assertTrue(self._run(db, "uuid-1", "OPTIWA-1", "ev-2"))
        sql, params = db.cur.sql[0]
        self.assertIn("event='resolved' AND event_id<>%s", sql)
        self.assertEqual(params, ("ev-2", "uuid-1", "uuid-1", "OPTIWA-1", "OPTIWA-1"))

    def test_no_resolved_means_not_resolved(self):
        self.assertFalse(self._run(_DB(rows=[]), "uuid-1", "OPTIWA-1", "ev-2"))

    def test_an_unreadable_store_keeps_the_reopen(self):
        self.assertTrue(self._run(_DB(fail=True), "uuid-1", "OPTIWA-1", "ev-2"))

    def test_no_identity_keeps_the_reopen(self):
        self.assertTrue(self._run(_DB(), "", "", "ev-2"))


class ReceiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.crm = _load_crm()
        from flask import Flask
        cls.app = Flask(__name__)
        cls.app.register_blueprint(cls.crm.bp)

    def setUp(self):
        crm = self.crm
        self.stored, self.sessions, self.status, self.jobs = [], [], [], []
        self.resolved = {"value": False}
        names = ("_verify_ket_signature", "_store_lifecycle_event", "_was_resolved",
                 "_process_session_lifecycle", "_set_lifecycle_status", "_audit",
                 "_enqueue_whatsapp_job", "_whatsapp_pref_allowed", "_attempt_whatsapp_job")
        self._saved = {n: getattr(crm, n) for n in names}
        crm._verify_ket_signature = lambda *a: True
        crm._store_lifecycle_event = lambda *a, **k: self.stored.append(a) or "claimed"
        crm._was_resolved = lambda *a: self.resolved["value"]
        crm._process_session_lifecycle = lambda *a: self.sessions.append(a)
        crm._set_lifecycle_status = lambda eid, st: self.status.append(st)
        crm._audit = lambda *a, **k: None
        crm._enqueue_whatsapp_job = lambda *a: self.jobs.append(a)
        crm._whatsapp_pref_allowed = lambda key: True
        crm._attempt_whatsapp_job = lambda *a: None

    def tearDown(self):
        for n, f in self._saved.items():
            setattr(self.crm, n, f)

    def _post(self, **fields):
        body = {"event": "reopened", "event_id": "e" * 32, "ticket_ref": "OPTIWA-1031",
                "name": "Asha", "phone": "919810012345", "email": "a@example.com"}
        body.update(fields)
        with self.app.test_client() as c:
            return c.post("/support/ticket_event", data=json.dumps(body),
                          content_type="application/json")

    def test_ticket_uid_is_read_first(self):
        r = self._post(event="resolved", ticket_uid="uid-new", ticket_id="uid-old")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.stored[0][3], "uid-new")
        r = self._post(event="resolved", ticket_id="uid-old")
        self.assertEqual(self.stored[1][3], "uid-old")

    def test_a_reopened_never_resolved_is_an_acceptance_with_no_whatsapp(self):
        r = self._post(ticket_uid="uid-1")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual((j["treated_as"], j["whatsapp"]), ("accepted", "not_sent"))
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.status, ["treated_as_accepted"])
        self.assertEqual(self.sessions[0][0], "accepted")

    def test_a_reopened_after_a_resolved_still_sends_the_whatsapp(self):
        self.resolved["value"] = True
        r = self._post(ticket_uid="uid-1")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("treated_as", r.get_json())
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.jobs[0][3], "support_ticket_reopened")
        self.assertEqual(self.sessions[0][0], "reopened")

    def test_a_native_accepted_is_stored_and_mapped_with_no_whatsapp(self):
        r = self._post(event="accepted", ticket_uid="uid-1")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual((j["status"], j["whatsapp"]), ("accepted", "not_sent"))
        self.assertNotIn("treated_as", j)
        self.assertEqual(self.stored[0][2], "accepted")
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.status, ["accepted"])
        self.assertEqual(self.sessions[0][0], "accepted")

    def test_a_null_phone_is_no_phone(self):
        r = self._post(event="resolved", ticket_uid="uid-1", phone=None)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["whatsapp"], "skipped_no_phone")
        self.assertEqual(self.jobs, [])

    def test_other_unknown_events_are_still_acked_and_ignored(self):
        r = self._post(event="assigned", ticket_uid="uid-1")
        self.assertEqual(r.get_json()["status"], "ignored")
        self.assertEqual(self.stored, [])


if __name__ == "__main__":
    unittest.main()
