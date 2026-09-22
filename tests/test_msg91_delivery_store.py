"""Tests for what one MSG91 delivery report does to the ledgers.

The parser tests prove the shapes are read and the endpoint tests prove what the
route answers. These prove the *storage* rule for a single report:

  - the same report delivered twice is stored once;
  - a report moves a ledger row forward only (no "sent" after "delivered",
    nothing after "read" or "failed");
  - a report for a face-scan invitation touches that invitation's delivery
    columns and nothing else — never its status, expiry or completion;
  - an unknown request id is kept for the audit trail and matches nothing.

    python3 -m unittest tests.test_msg91_delivery_store
"""
import importlib.util
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SAVED = {}
_TOUCHED = ("flask_mail", "flaskr", "flaskr.db", "flaskr.mail",
            "flaskr.captcha", "flaskr.crm_store_under_test")


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for key, val in attrs.items():
        setattr(mod, key, val)
    sys.modules[name] = mod
    return mod


def _load_crm():
    if "flask_mail" not in sys.modules:
        _stub("flask_mail", Message=object)
    pkg = types.ModuleType("flaskr")
    pkg.__path__ = [REPO]
    sys.modules["flaskr"] = pkg
    _stub("flaskr.db", get_db=lambda *a, **k: None)
    _stub("flaskr.mail", send_contact_email=lambda *a, **k: None,
          create_ticket_in_db=lambda *a, **k: None)
    _stub("flaskr.captcha", CaptchaGenerator=object)
    spec = importlib.util.spec_from_file_location(
        "flaskr.crm_store_under_test", os.path.join(REPO, "crm.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["flaskr.crm_store_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


crm = None


def setUpModule():
    global crm
    for name in _TOUCHED:
        _SAVED[name] = sys.modules.get(name)
    crm = _load_crm()
    crm._KET_SCHEMA_READY = True


def tearDownModule():
    for name, mod in _SAVED.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


class _FakeDB:
    """Three tables, just enough SQL to answer the store function."""

    def __init__(self, outbox=None, invite=None):
        self.events = []            # msg91_delivery_events rows
        self.outbox = outbox        # one whatsapp_delivery_log row or None
        self.invite = invite        # one face_scan_invites row or None
        self.invite_table = True
        self.commits = 0
        self.updates = []

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1


class _FakeCursor:
    def __init__(self, db):
        self.db = db
        self._row = None

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        db = self.db
        self._row = None
        if q.startswith("SELECT id FROM msg91_delivery_events"):
            rid, status, ts = params
            for ev in db.events:
                if (ev["msg91_request_id"], ev["status"],
                        ev["provider_ts"] or '') == (rid, status, ts):
                    self._row = {"id": 1}
        elif q.startswith("SELECT event_id, ticket_ref, recipient, template_name, status FROM whatsapp_delivery_log"):
            if db.outbox and db.outbox["msg91_request_id"] == params[0]:
                self._row = dict(db.outbox)
        elif q.startswith("SELECT id, delivery_status FROM face_scan_invites"):
            if not db.invite_table:
                raise RuntimeError("Table 'face_scan_invites' doesn't exist")
            if db.invite and db.invite["delivery_ref"] == params[0]:
                self._row = {"id": db.invite["id"],
                             "delivery_status": db.invite["delivery_status"]}
        elif q.startswith("INSERT INTO msg91_delivery_events"):
            keys = ("msg91_request_id", "event_id", "ticket_ref", "recipient",
                    "template_name", "status", "failure_reason", "provider_ts")
            db.events.append(dict(zip(keys, params)))
        elif q.startswith("UPDATE whatsapp_delivery_log"):
            db.updates.append(("outbox", params[0]))
            db.outbox["status"] = params[0]
        elif q.startswith("UPDATE face_scan_invites"):
            db.updates.append(("invite", params[0]))
            db.invite["delivery_status"] = params[0]
            if params[0] == "FAILED":
                db.invite["delivery_error"] = params[3]
        else:
            raise AssertionError("unexpected SQL: " + q)

    def fetchone(self):
        return self._row

    def close(self):
        pass


def _install(db):
    sys.modules["flaskr.db"].get_db = lambda *a, **k: db


def _invite(**over):
    row = {"id": 4, "delivery_ref": "rid-face", "delivery_status": "SENT",
           "delivery_error": None, "status": "SCANNING",
           "expires_at": "2026-09-22 13:10:00", "completed_at": None}
    row.update(over)
    return row


class TransitionRuleTests(unittest.TestCase):
    def test_forward_moves_only(self):
        ok = crm.delivery_transition_allowed
        self.assertTrue(ok("pending", "sent"))
        self.assertTrue(ok("SENT", "delivered"))
        self.assertTrue(ok("delivered", "read"))
        self.assertTrue(ok("sent", "failed"))
        self.assertFalse(ok("delivered", "sent"))
        self.assertFalse(ok("delivered", "delivered"))

    def test_read_and_failed_are_final(self):
        ok = crm.delivery_transition_allowed
        self.assertFalse(ok("read", "failed"))
        self.assertFalse(ok("read", "delivered"))
        self.assertFalse(ok("FAILED", "delivered"))
        self.assertFalse(ok("failed", "read"))

    def test_an_unknown_status_never_moves_a_row(self):
        self.assertFalse(crm.delivery_transition_allowed("sent", "wibble"))
        self.assertFalse(crm.delivery_transition_allowed("sent", ""))


class DuplicateTests(unittest.TestCase):
    def test_the_same_report_twice_is_stored_once(self):
        db = _FakeDB(outbox={"msg91_request_id": "r1", "event_id": "e", "ticket_ref": "t",
                             "recipient": "91x", "template_name": "tpl", "status": "sent"})
        _install(db)
        self.assertTrue(crm._store_delivery_event("r1", "delivered", "", "1700000000"))
        self.assertTrue(crm._store_delivery_event("r1", "delivered", "", "1700000000"))
        self.assertEqual(len(db.events), 1)
        self.assertEqual(db.updates, [("outbox", "delivered")])

    def test_a_different_status_for_the_same_id_is_a_new_event(self):
        db = _FakeDB(outbox={"msg91_request_id": "r1", "event_id": "e", "ticket_ref": "t",
                             "recipient": "91x", "template_name": "tpl", "status": "sent"})
        _install(db)
        crm._store_delivery_event("r1", "delivered", "", "1")
        crm._store_delivery_event("r1", "read", "", "2")
        self.assertEqual([e["status"] for e in db.events], ["delivered", "read"])
        self.assertEqual(db.outbox["status"], "read")


class LegalTransitionTests(unittest.TestCase):
    def test_a_late_sent_after_delivered_is_logged_but_does_not_move_the_row(self):
        db = _FakeDB(outbox={"msg91_request_id": "r1", "event_id": "e", "ticket_ref": "t",
                             "recipient": "91x", "template_name": "tpl", "status": "delivered"})
        _install(db)
        crm._store_delivery_event("r1", "sent", "", "9")
        self.assertEqual(len(db.events), 1)
        self.assertEqual(db.outbox["status"], "delivered")
        self.assertEqual(db.updates, [])

    def test_a_failure_after_read_does_not_undo_the_read(self):
        db = _FakeDB(outbox={"msg91_request_id": "r1", "event_id": "e", "ticket_ref": "t",
                             "recipient": "91x", "template_name": "tpl", "status": "read"})
        _install(db)
        crm._store_delivery_event("r1", "failed", "expired", "9")
        self.assertEqual(db.outbox["status"], "read")
        self.assertEqual(db.updates, [])


class FaceScanInviteTests(unittest.TestCase):
    def test_delivered_reaches_the_invitation_it_was_sent_for(self):
        db = _FakeDB(invite=_invite())
        _install(db)
        self.assertTrue(crm._store_delivery_event("rid-face", "delivered", "", "5"))
        self.assertEqual(db.invite["delivery_status"], "DELIVERED")
        self.assertEqual(db.events[0]["template_name"], "face_scan_request")

    def test_a_failed_whatsapp_is_a_failed_notification_and_nothing_else(self):
        inv = _invite()
        before = {k: inv[k] for k in ("status", "expires_at", "completed_at")}
        db = _FakeDB(invite=inv)
        _install(db)
        crm._store_delivery_event("rid-face", "failed", "number not on whatsapp", "5")
        self.assertEqual(inv["delivery_status"], "FAILED")
        self.assertEqual(inv["delivery_error"], "number not on whatsapp")
        self.assertEqual({k: inv[k] for k in before}, before)
        self.assertEqual([u[0] for u in db.updates], ["invite"])

    def test_read_after_delivered_moves_forward_and_not_back(self):
        db = _FakeDB(invite=_invite(delivery_status="DELIVERED"))
        _install(db)
        crm._store_delivery_event("rid-face", "read", "", "6")
        self.assertEqual(db.invite["delivery_status"], "READ")
        crm._store_delivery_event("rid-face", "delivered", "", "7")
        self.assertEqual(db.invite["delivery_status"], "READ")

    def test_a_site_without_the_invites_table_still_stores_the_event(self):
        db = _FakeDB()
        db.invite_table = False
        _install(db)
        self.assertFalse(crm._store_delivery_event("r-unknown", "delivered", "", "1"))
        self.assertEqual(len(db.events), 1)


class UnknownIdTests(unittest.TestCase):
    def test_an_unknown_id_is_kept_for_audit_and_matches_nothing(self):
        db = _FakeDB()
        _install(db)
        self.assertFalse(crm._store_delivery_event("nobody", "sent", "", "1"))
        self.assertEqual(len(db.events), 1)
        self.assertEqual(db.updates, [])


if __name__ == "__main__":
    unittest.main()
