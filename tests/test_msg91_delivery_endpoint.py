"""Tests for POST /support/msg91_delivery_event as a route, not as a parser.

The parser tests (tests/test_msg91_delivery_reports.py) prove every provider
shape is understood. These prove what the endpoint *answers*, which is what
decides whether the webhook stays alive and whether a redelivery duplicates the
ledger:

  - a batch that stored some reports answers 200, even if one report failed,
    because MSG91 redelivers the whole body and the stored ones would be
    recorded twice;
  - a batch that stored nothing answers 503, the one case where redelivery
    cannot duplicate anything;
  - an unparseable body is acknowledged;
  - the shared token is required, not optional: this route writes to the
    delivery ledger and can flip an outbox row's status.

crm.py's heavy dependencies are stubbed, but flask is real so the blueprint can
be exercised through a test client.

    python3 -m unittest tests.test_msg91_delivery_endpoint
"""
import importlib.util
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SAVED = {}
_TOUCHED = ("flask_mail", "flaskr", "flaskr.db", "flaskr.mail",
            "flaskr.captcha", "flaskr.crm_endpoint_under_test")


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for key, val in attrs.items():
        setattr(mod, key, val)
    sys.modules[name] = mod
    return mod


def _load_crm():
    # flask_mail is not installed in the isolated env; crm.py only needs Message.
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
        "flaskr.crm_endpoint_under_test", os.path.join(REPO, "crm.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["flaskr.crm_endpoint_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


crm = None
Flask = None


def setUpModule():
    global crm, Flask
    for name in _TOUCHED:
        _SAVED[name] = sys.modules.get(name)
    crm = _load_crm()
    from flask import Flask as _Flask
    Flask = _Flask


def tearDownModule():
    for name, mod in _SAVED.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


TOKEN = "test-token"


class _Store:
    """Stands in for _store_delivery_event: records calls, fails chosen ids.

    Returns True for a 'known' request id (the outbox row exists), which is what
    the endpoint counts as matched.
    """

    def __init__(self, fail_ids=(), known_ids=()):
        self.fail_ids = set(fail_ids)
        self.known_ids = set(known_ids)
        self.stored = []

    def __call__(self, request_id, status, failure_reason, provider_ts):
        if request_id in self.fail_ids:
            raise RuntimeError("simulated storage failure")
        self.stored.append((request_id, status))
        return request_id in self.known_ids


class _Audit:
    def __init__(self):
        self.calls = []

    def __call__(self, event, **kw):
        self.calls.append((event, kw))


class DeliveryEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['MSG91_DELIVERY_TOKEN'] = TOKEN
        self.app.register_blueprint(crm.bp)
        self.client = self.app.test_client()
        self._real_store = crm._store_delivery_event
        self._real_audit = crm._audit
        self.audit = _Audit()
        crm._audit = self.audit

    def tearDown(self):
        crm._store_delivery_event = self._real_store
        crm._audit = self._real_audit

    def _post(self, body, token=TOKEN, header=True):
        headers = {"X-MSG91-Token": token} if (token and header) else {}
        url = "/support/msg91_delivery_event"
        if token and not header:
            url += "?token=%s" % token
        return self.client.post(url, json=body, headers=headers)

    def _install(self, store):
        crm._store_delivery_event = store

    # ── retry semantics ────────────────────────────────────────────────────
    def test_a_partly_stored_batch_is_acknowledged(self):
        # MSG91 redelivers the whole body, so asking it to retry after two of
        # three reports were committed would record those two a second time.
        store = _Store(fail_ids={"r2"}, known_ids={"r1", "r3"})
        self._install(store)
        r = self._post({"messages": [{"requestId": "r1", "status": "sent"},
                                     {"requestId": "r2", "status": "delivered"},
                                     {"requestId": "r3", "status": "read"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["stored"], 2)
        self.assertEqual(r.get_json()["failed"], 1)
        self.assertEqual([rid for rid, _s in store.stored], ["r1", "r3"])

    def test_a_batch_that_stored_nothing_is_retryable(self):
        # Nothing was committed, so redelivery cannot duplicate anything and a
        # 503 buys a real second chance at the report.
        store = _Store(fail_ids={"r1", "r2"})
        self._install(store)
        r = self._post({"messages": [{"requestId": "r1", "status": "sent"},
                                     {"requestId": "r2", "status": "failed"}]})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(store.stored, [])

    def test_a_healthy_batch_reports_matched_separately_from_stored(self):
        store = _Store(known_ids={"r1"})
        self._install(store)
        r = self._post({"messages": [{"requestId": "r1", "status": "delivered"},
                                     {"requestId": "unknown", "status": "sent"}]})
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual((body["stored"], body["matched"], body["failed"]),
                         (2, 1, 0))

    def test_an_unparseable_body_is_acknowledged_not_refused(self):
        # A non-2xx answer pauses the webhook, which loses every later report.
        self._install(_Store())
        r = self._post({"hello": "world"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "ignored")

    def test_a_flat_sms_shaped_report_still_works(self):
        store = _Store(known_ids={"abc"})
        self._install(store)
        r = self._post({"request_id": "abc", "status": "DELIVERED"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(store.stored, [("abc", "delivered")])

    # ── authentication ─────────────────────────────────────────────────────
    def test_a_wrong_token_is_refused(self):
        self._install(_Store())
        r = self._post({"request_id": "abc", "status": "sent"}, token="nope")
        self.assertEqual(r.status_code, 401)

    def test_the_token_may_still_arrive_in_the_query_string(self):
        # Production is configured this way today; the header is preferred
        # because a query string lands in the access log, but dropping support
        # would pause the live webhook.
        store = _Store(known_ids={"abc"})
        self._install(store)
        r = self._post({"request_id": "abc", "status": "sent"}, header=False)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(store.stored, [("abc", "sent")])

    def test_no_configured_token_refuses_instead_of_accepting_anyone(self):
        # This route writes to the delivery ledger and can flip an outbox row to
        # delivered/failed. With no token there is nothing to authenticate, so it
        # must fail closed: MSG91 pauses a refused webhook loudly, where an open
        # writer is silent.
        self.app.config['MSG91_DELIVERY_TOKEN'] = ''
        store = _Store()
        self._install(store)
        r = self.client.post("/support/msg91_delivery_event",
                             json={"request_id": "abc", "status": "sent"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(store.stored, [])


if __name__ == "__main__":
    unittest.main()
