"""Coming back from a Razorpay payment link must not 404 on your own order.

``/success/<order_id>`` may only be read by the customer who owns the order —
that guard stops an ``order_id`` being enumerated for somebody else's name,
address and prescription. But a payment link is paid from whatever browser the
shopper happens to have: no session, or a session belonging to the operator who
created the order. Every payment-link payer therefore got a 404 on the page that
confirms their money arrived (order ZYWMUN-263831, 2026-09-02).

Razorpay signs the return parameters with the key secret, and the signed payload
names the order. That proof is stronger than a session cookie and it opens
exactly one order, so it authorises the page. These tests pin what the proof has
to say before it counts, and that an invalid one changes nothing.
"""
import hashlib
import hmac
import importlib.util
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SECRET = "rzp-key-secret-for-tests"
ORDER = "ZYWMUN-263831"
LINK = "plink_TWK3kmByvDjepG"
PAYMENT = "pay_TX1B7daSub7HQ6"


def _stub(name, **attrs):
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod


def _payments():
    """``payments`` without its gateway SDKs, which these tests never call."""
    _stub("razorpay", Client=object)
    _stub("paytmchecksum", PaytmChecksum=object)
    _stub("Crypto")
    _stub("Crypto.Cipher", AES=object)
    _stub("Crypto.Random", get_random_bytes=lambda n: b"\0" * n)
    sys.modules["Crypto"].Cipher = sys.modules["Crypto.Cipher"]
    sys.modules["Crypto"].Random = sys.modules["Crypto.Random"]
    pkg_name = "payments_under_test"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [REPO]
        sys.modules[pkg_name] = pkg
    for name in ("razorpay_events", "payments"):
        full = "%s.%s" % (pkg_name, name)
        spec = importlib.util.spec_from_file_location(
            full, os.path.join(REPO, "%s.py" % name))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
    return sys.modules["%s.payments" % pkg_name]


payments = _payments()


def signature(link_id=LINK, reference=ORDER, status="paid", payment=PAYMENT,
              secret=SECRET):
    msg = "%s|%s|%s|%s" % (link_id, reference, status, payment)
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def args(reference=ORDER, status="paid", link_id=LINK, payment=PAYMENT,
         sig=None):
    """The query string Razorpay sends the shopper back with."""
    return {
        "razorpay_payment_id": payment,
        "razorpay_payment_link_id": link_id,
        "razorpay_payment_link_reference_id": reference,
        "razorpay_payment_link_status": status,
        "razorpay_signature": sig if sig is not None else signature(
            link_id, reference, status, payment),
    }


def verify(order_id, query):
    return payments.verify_razorpay_payment_link(order_id, query,
                                                 key_secret=SECRET)


class PaymentLinkProofTests(unittest.TestCase):

    def test_the_signed_return_of_a_paid_link_authorises_that_order(self):
        self.assertTrue(verify(ORDER, args()))

    def test_a_signature_for_another_order_does_not_open_this_one(self):
        """The whole point of the guard: order A's proof is not order B's."""
        other = args(reference="OTHER-999999")
        self.assertTrue(verify("OTHER-999999", other))
        self.assertFalse(verify(ORDER, other))

    def test_a_tampered_signature_proves_nothing(self):
        self.assertFalse(verify(ORDER, args(sig="6f0a" + "0" * 60)))
        self.assertFalse(verify(ORDER, args(sig="")))

    def test_a_signature_from_somebody_elses_secret_proves_nothing(self):
        forged = args(sig=signature(secret="not-our-secret"))
        self.assertFalse(verify(ORDER, forged))

    def test_a_link_that_was_not_paid_authorises_nothing(self):
        """A cancelled or expired link is signed too, and it is not a payment."""
        for status in ("cancelled", "expired", "created", "Paid", ""):
            self.assertFalse(verify(ORDER, args(status=status)), status)

    def test_a_return_missing_any_parameter_proves_nothing(self):
        for field in ("razorpay_payment_id", "razorpay_payment_link_id",
                      "razorpay_signature"):
            query = args()
            query[field] = ""
            self.assertFalse(verify(ORDER, query), field)

    def test_an_ordinary_page_view_carries_no_proof(self):
        self.assertFalse(verify(ORDER, {}))

    def test_with_no_secret_configured_nothing_is_authorised(self):
        self.assertFalse(payments.verify_razorpay_payment_link(
            ORDER, args(), key_secret=""))

    def test_the_signature_is_compared_in_constant_time(self):
        with open(os.path.join(REPO, "payments.py")) as fh:
            source = fh.read()
        block = source.split("def verify_razorpay_payment_link", 1)[1]
        block = block.split("\ndef ", 1)[0]
        self.assertIn("hmac.compare_digest", block)
        self.assertNotIn("== signature", block)


class SuccessRouteGuardTests(unittest.TestCase):
    """``models.py`` is not importable without the app; read the guard instead."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "models.py")) as fh:
            source = fh.read()
        cls.route = source.split("def success(order_id):", 1)[1][:2500]
        cls.source = source

    def test_the_page_accepts_the_payment_link_proof(self):
        self.assertIn("verify_razorpay_payment_link(order_id, request.args)",
                      self.route)
        self.assertIn("verify_razorpay_payment_link", self.source.splitlines()[4])

    def test_an_unproven_visit_still_meets_the_owner_check(self):
        """No proof: sign in, then own the order, exactly as before."""
        after = self.route.split("verify_razorpay_payment_link", 1)[1]
        self.assertIn("auth.login", after)
        self.assertIn("o.customer_id = %s OR c.customer_email = %s", after)
        self.assertIn("ACTIVITY:ORDER_SUCCESS_DENIED", after)
        self.assertIn("abort(404)", after)

    def test_a_payment_link_view_is_logged_as_one(self):
        self.assertIn("ACTIVITY:ORDER_SUCCESS_PAYLINK", self.route)

    def test_the_page_writes_no_payment_state(self):
        """Persistence is the webhook's job; this page only reads."""
        for wrote in ("INSERT INTO payment_collector", "append_status(",
                      "apply_paid_order("):
            self.assertNotIn(wrote, self.route, wrote)


class Denied(Exception):
    """``abort(404)``."""

    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


class FakeCursor:
    """Answers the ownership query: one row when the visitor owns the order."""

    def __init__(self, owns):
        self._owns = owns
        self.queries = []
        self._row = None

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        self._row = {"order_id": params[0]} if self._owns else None

    def fetchone(self):
        return self._row


class FakeRequest:
    def __init__(self, query):
        self.args = query
        self.headers = {}
        self.remote_addr = "203.0.113.7"
        self.host = "in.optiwar.com"
        self.path = "/success/" + ORDER


def _guard():
    """The real guard block from ``success()``, callable without app or DB."""
    with open(os.path.join(REPO, "models.py")) as fh:
        source = fh.read()
    body = source.split("def success(order_id):", 1)[1]
    start = body.index("    _uid = session.get('user_id')")
    end = body.index("            abort(404)\n") + len("            abort(404)\n")
    block = "".join("    " + line[4:] + "\n"
                    for line in body[start:end].split("\n"))
    src = ("def guard(order_id, session, request, cursor, flash, redirect,\n"
           "          url_for, current_app, abort, verify_razorpay_payment_link):\n"
           + block + "    return 200\n")
    ns = {}
    exec(src, ns)  # noqa: S102 - first-party source, read from the repo
    return ns["guard"]


guard_block = _guard()


class Logger:
    def __init__(self):
        self.lines = []

    def info(self, line):
        self.lines.append(line)

    warning = info


def visit(query, session_data=None, owns=False):
    """Run the real guard; return (outcome, cursor, log lines)."""
    logger = Logger()
    cursor = FakeCursor(owns)
    app = types.SimpleNamespace(logger=logger)

    def abort(code):
        raise Denied(code)

    try:
        outcome = guard_block(
            ORDER, session_data or {}, FakeRequest(query), cursor,
            lambda msg: None, lambda url: ("redirect", url),
            lambda endpoint, **kw: endpoint, app, abort,
            lambda order_id, a: payments.verify_razorpay_payment_link(
                order_id, a, key_secret=SECRET))
    except Denied as denied:
        outcome = denied.code
    return outcome, cursor, logger.lines


class SuccessGuardBehaviourTests(unittest.TestCase):
    """What the page does, run against the guard as it stands in models.py."""

    def test_a_paid_link_return_with_no_session_is_let_through(self):
        outcome, cursor, log = visit(args())
        self.assertEqual(outcome, 200)
        self.assertEqual(cursor.queries, [])  # the owner check is not even asked
        self.assertIn("ACTIVITY:ORDER_SUCCESS_PAYLINK", log[0])
        self.assertIn(PAYMENT, log[0])

    def test_a_paid_link_return_beats_somebody_elses_session(self):
        """The reported incident: ops' session in the shopper's browser."""
        outcome, _, _ = visit(args(), session_data={"user_id": 999})
        self.assertEqual(outcome, 200)

    def test_a_proof_for_another_order_is_refused(self):
        outcome, _, log = visit(args(reference="OTHER-999999"),
                                session_data={"user_id": 999})
        self.assertEqual(outcome, 404)
        self.assertIn("ACTIVITY:ORDER_SUCCESS_DENIED", log[0])

    def test_a_cancelled_link_authorises_nothing(self):
        outcome, _, _ = visit(args(status="cancelled"))
        self.assertEqual(outcome, ("redirect", "auth.login"))

    def test_a_tampered_signature_falls_back_to_the_session_rules(self):
        bad = args(sig="6f0a" + "0" * 60)
        self.assertEqual(visit(bad)[0], ("redirect", "auth.login"))
        self.assertEqual(visit(bad, session_data={"user_id": 999})[0], 404)
        self.assertEqual(
            visit(bad, session_data={"user_id": 647}, owns=True)[0], 200)

    def test_the_owner_anonymous_and_wrong_owner_cases_are_unchanged(self):
        self.assertEqual(visit({})[0], ("redirect", "auth.login"))
        self.assertEqual(visit({}, session_data={"user_id": 999})[0], 404)
        self.assertEqual(
            visit({}, session_data={"user_email": "Amreen60@hotmail.com"},
                  owns=True)[0], 200)


if __name__ == "__main__":
    unittest.main()
