"""The origin guard must not stand in front of provider webhooks.

Production runs the guard with ``CSRF_ENFORCE=true``. A provider posts from its
own servers, so its delivery carries neither Origin nor Referer — the exact
shape the guard treats as an attack. Without an exemption the view never runs
and every payment Razorpay reports is silently dropped with a 403, which is
indistinguishable from "no one has paid" until stock and books disagree.

These tests drive the real guard over a miniature app: no database, no gateway,
so what they prove is only the routing decision, which is what broke.
"""
import unittest

from flask import Blueprint, Flask, jsonify

# Requests are made as the storefront so that newer Flask's own host check
# (TRUSTED_HOSTS) is satisfied and the only decision left is the guard's.
BASE = "https://optiwar.com"

from csrf_guard import CSRF_EXEMPT_ENDPOINTS, evaluate, init_csrf_guard


def _app(enforce=True):
    """A Flask app whose endpoint names match the real ones."""
    app = Flask(__name__)
    app.config.update(CSRF_ENFORCE=enforce, TRUSTED_HOSTS=["optiwar.com"])
    bp = Blueprint("main", __name__)

    @bp.route("/razorpay/webhook", methods=["POST"])
    def razorpay_webhook():
        # Stands in for the signature check: the first thing the real view does
        # with an unsigned body is refuse it, so a 400 here means "reached".
        return jsonify(reached=True), 400

    @bp.route("/razorpay/verify", methods=["POST"])
    def razorpay_verify():
        return jsonify(reached=True), 200

    app.register_blueprint(bp)
    init_csrf_guard(app)
    return app


class WebhookReachabilityTests(unittest.TestCase):

    def test_a_delivery_with_no_origin_reaches_the_webhook(self):
        with _app().test_client() as c:
            r = c.post("/razorpay/webhook", data="{}",
                       content_type="application/json", base_url=BASE)
        self.assertEqual(r.status_code, 400)          # not 403
        self.assertTrue(r.get_json()["reached"])

    def test_a_foreign_origin_also_reaches_the_webhook(self):
        # Razorpay is entitled to send whatever headers it likes; the signature
        # is the authentication, so the header cannot be allowed to matter.
        with _app().test_client() as c:
            r = c.post("/razorpay/webhook", data="{}",
                       content_type="application/json", base_url=BASE,
                       headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 400)

    def test_the_browser_verify_route_is_still_guarded(self):
        # razorpay_verify carries the customer's session cookie, so it must keep
        # failing closed. Exempting a blueprint instead of an endpoint would
        # have taken this with it.
        with _app().test_client() as c:
            r = c.post("/razorpay/verify", data="{}",
                       content_type="application/json", base_url=BASE)
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("main.razorpay_verify", CSRF_EXEMPT_ENDPOINTS)

    def test_the_guard_still_rejects_a_missing_origin_in_general(self):
        # The exemption is per-endpoint, not a weakening of the decision.
        self.assertEqual(evaluate(None, None, "https://optiwar.com/",
                                  ["optiwar.com"]),
                         ("missing-origin-referer", True))


if __name__ == "__main__":
    unittest.main()
