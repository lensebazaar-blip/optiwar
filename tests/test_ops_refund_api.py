"""The refund API's edges: who may call it, and what it refuses to do.

The money arithmetic is tested against a real database in ``test_refunds``.
What is left here is everything a caller controls — the credential, the named
operator, the idempotency key, a body with no amount in it — and the rule that
none of it may reach the provider unless all of it is right. No database and no
network: the provider and the connection are stubs, so a test that "succeeds"
proves routing and authorisation, never a payment.
"""
import importlib
import importlib.util
import json
import os
import sys
import types
import unittest

from flask import Blueprint, Flask

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

BASE = "https://optiwar.com"
TOKEN = "ops-refund-token-for-tests"
PKG = "refund_api_under_test"


def _load_module(pkg_name, name):
    spec = importlib.util.spec_from_file_location(
        "%s.%s" % (pkg_name, name), os.path.join(REPO, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _package():
    """``ops_refunds`` with its two package siblings, without the real package.

    It imports ``.db``, which imports MySQLdb and expects an app context; the
    connection is the one thing these tests must not have.
    """
    if PKG in sys.modules:
        return sys.modules[PKG]
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [REPO]
    sys.modules[PKG] = pkg

    db_stub = types.ModuleType("%s.db" % PKG)
    db_stub.get_db = lambda: FakeDB.current
    sys.modules[db_stub.__name__] = db_stub

    _load_module(PKG, "refunds")
    _load_module(PKG, "ops_refunds")
    return pkg


_package()
refunds = sys.modules["%s.refunds" % PKG]
ops_refunds = sys.modules["%s.ops_refunds" % PKG]


class FakeCursor:
    """Answers just enough for the API to reach a decision."""

    current = None

    def __init__(self):
        self.statements = []
        self.lastrowid = 1

    def execute(self, sql, args=None):
        self.statements.append((" ".join(sql.split()), args))

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class FakeDB:
    current = None

    def __init__(self):
        self.cursors = []
        self.commits = 0

    def cursor(self):
        cur = FakeCursor()
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class FakeProvider:
    """Records whether the provider was touched at all. It must not be."""

    def __init__(self):
        self.calls = []

    def payment(self, payment_ref):
        self.calls.append(("payment", payment_ref))
        return {"amount": 99900, "currency": "INR", "status": "captured",
                "amount_refunded": 0, "gateway": "razorpay"}

    def refund(self, *args, **kw):
        self.calls.append(("refund", args))
        raise AssertionError("the provider must not be called in these tests")

    def refund_status(self, provider_refund_id):
        self.calls.append(("refund_status", provider_refund_id))
        return {"id": provider_refund_id, "status": "processed"}


def _app(token=TOKEN):
    app = Flask(__name__)
    app.config.update(TRUSTED_HOSTS=["optiwar.com"])
    app.secret_key = "test-only"
    if token is not None:
        app.config["OPS_REFUND_API_TOKEN"] = token
    bp = Blueprint("main", __name__)
    ops_refunds.register(bp)
    app.register_blueprint(bp)
    return app


class OpsRefundApiTest(unittest.TestCase):

    def setUp(self):
        FakeDB.current = FakeDB()
        self.provider = FakeProvider()
        self._real_provider = ops_refunds._provider
        ops_refunds._provider = lambda: self.provider
        refunds._SCHEMA_READY = True      # no DDL against a stub cursor

    def tearDown(self):
        ops_refunds._provider = self._real_provider

    def _post(self, client, body, headers=None, order_id="OW-TEST-1"):
        head = {"Authorization": "Bearer %s" % TOKEN,
                "X-Ops-Operator": "ops@lensbazaar",
                "Idempotency-Key": "OW-TEST-1/refund/abc"}
        head.update(headers or {})
        for key, value in list(head.items()):
            if value is None:
                del head[key]
        return client.post("/api/ops/orders/%s/refund" % order_id,
                           data=json.dumps(body),
                           content_type="application/json",
                           headers=head, base_url=BASE)

    # ─── the credential ──────────────────────────────────────────────

    def test_no_credential_is_401_on_every_endpoint(self):
        with _app().test_client() as c:
            self.assertEqual(
                c.get("/api/ops/orders/OW-TEST-1/refund/preview",
                      base_url=BASE).status_code, 401)
            self.assertEqual(
                c.get("/api/ops/refunds/OW-TEST-1/refund/abc",
                      base_url=BASE).status_code, 401)
            self.assertEqual(
                self._post(c, {}, headers={"Authorization": None}).status_code, 401)
        self.assertEqual(self.provider.calls, [])

    def test_the_wrong_credential_is_401(self):
        with _app().test_client() as c:
            r = self._post(c, {}, headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status_code, 401)

    def test_an_unconfigured_token_disables_the_api_rather_than_opening_it(self):
        # No OPS_REFUND_API_TOKEN anywhere: an empty comparison must not pass.
        for header in ("Bearer ", "Bearer anything"):
            with _app(token=None).test_client() as c:
                r = self._post(c, {}, headers={"Authorization": header})
            self.assertEqual(r.status_code, 401)

    def test_the_session_cookie_is_not_a_refund_credential(self):
        # /ops accepts an admin session; refunding money does not.
        app = _app()
        with app.test_client() as c:
            with c.session_transaction(base_url=BASE) as sess:
                sess["user_email"] = "admin@optiwar.com"
            r = self._post(c, {"amount_minor": 1}, headers={"Authorization": None})
        self.assertEqual(r.status_code, 401)

    # ─── the caller's obligations ────────────────────────────────────

    def test_a_refund_with_no_named_human_is_refused(self):
        with _app().test_client() as c:
            r = self._post(c, {"amount_minor": 99900, "currency": "INR"},
                           headers={"X-Ops-Operator": None})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "operator_required")
        self.assertEqual(self.provider.calls, [])

    def test_a_refund_with_no_idempotency_key_is_refused(self):
        with _app().test_client() as c:
            r = self._post(c, {"amount_minor": 99900, "currency": "INR"},
                           headers={"Idempotency-Key": None})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "idempotency_key_required")
        self.assertEqual(self.provider.calls, [])

    def test_the_key_may_travel_in_the_body_instead_of_the_header(self):
        with _app().test_client() as c:
            r = self._post(c, {"amount_minor": 99900, "currency": "INR",
                               "idempotency_key": "OW-TEST-1/refund/xyz"},
                           headers={"Idempotency-Key": None})
        # Rejected for the order, not for the key: the stub database has no order.
        self.assertEqual(r.get_json()["error"], "order_not_found")

    def test_a_body_with_no_amount_never_reaches_the_provider(self):
        with _app().test_client() as c:
            r = self._post(c, {"currency": "INR", "reason_code": "CUSTOMER_REFUND"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"], "order_not_found")
        self.assertEqual(self.provider.calls, [])

    # ─── shape of the answers ────────────────────────────────────────

    def test_a_missing_order_is_404_on_preview(self):
        with _app().test_client() as c:
            r = c.get("/api/ops/orders/NOPE/refund/preview", base_url=BASE,
                      headers={"Authorization": "Bearer %s" % TOKEN})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"], "order_not_found")

    def test_an_unknown_key_is_404_on_tracking(self):
        with _app().test_client() as c:
            r = c.get("/api/ops/refunds/OW-TEST-1/refund/abc", base_url=BASE,
                      headers={"Authorization": "Bearer %s" % TOKEN})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"], "refund_not_found")

    def test_the_ledger_json_names_the_payment_it_refunded(self):
        # Reconciliation starts from the provider payment, so tracking has to say
        # which payment the refund came out of.
        row = {'refund_id': 1, 'order_id': 'TEST-1', 'payment_ref': 'pay_abc',
               'provider_refund_id': 'rfnd_1', 'amount_minor': 99900,
               'currency': 'INR', 'status': 'PROCESSED', 'provider_state': 'processed',
               'refund_type': 'FULL', 'reason_code': 'CUSTOMER_REFUND',
               'idempotency_key': 'k', 'requested_by': 'ops', 'service_identity': 'eu',
               'requested_at': '2026-01-01 00:00:00', 'completed_at': None,
               'error_text': None}
        self.assertEqual(ops_refunds._ledger_json(row)['payment_ref'], 'pay_abc')

    def test_a_provider_outage_is_502_not_a_silent_success(self):
        def broken():
            raise ops_refunds.ProviderError("provider unreachable")
        ops_refunds._provider = broken
        with _app().test_client() as c:
            r = c.get("/api/ops/orders/OW-TEST-1/refund/preview", base_url=BASE,
                      headers={"Authorization": "Bearer %s" % TOKEN})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json()["error"], "provider_unavailable")

    def test_the_provider_error_carries_no_credential(self):
        class Resp:
            status_code = 400

            @staticmethod
            def json():
                return {"error": {"description": "The amount is invalid",
                                  "code": "BAD_REQUEST_ERROR"}}

        message = ops_refunds._provider_message(Resp)
        self.assertIn("The amount is invalid", message)
        self.assertNotIn("rzp_", message)
        self.assertNotIn("Authorization", message)

    def test_the_post_endpoint_is_exempt_from_the_browser_origin_guard(self):
        # It authenticates with a Bearer credential, not a cookie, so the guard
        # would only ever block the legitimate caller.
        import csrf_guard
        self.assertIn("main.ops_refund_execute", csrf_guard.CSRF_EXEMPT_ENDPOINTS)


if __name__ == "__main__":
    unittest.main()
