"""The origin guard must not stand in front of provider webhooks.

Production runs the guard with ``CSRF_ENFORCE=true``. A provider posts from its
own servers, so its delivery carries neither Origin nor Referer — the exact
shape the guard treats as an attack. Without an exemption the view never runs
and every payment Razorpay reports is silently dropped with a 403, which is
indistinguishable from "no one has paid" until stock and books disagree.

These tests drive the real guard over a miniature app: no database, no gateway,
so what they prove is only the routing decision, which is what broke.
"""
import importlib.util
import os
import re
import sys
import unittest
from unittest import mock

from flask import Blueprint, Flask, jsonify

# Requests are made as the storefront so that newer Flask's own host check
# (TRUSTED_HOSTS) is satisfied and the only decision left is the guard's.
BASE = "https://optiwar.com"

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

_pricing_spec = importlib.util.spec_from_file_location(
    "pricing_for_csrf_test", os.path.join(REPO, "pricing.py"))
pricing = importlib.util.module_from_spec(_pricing_spec)
_pricing_spec.loader.exec_module(pricing)

from csrf_guard import (BEARER_EXEMPT_ENDPOINTS, CSRF_EXEMPT_ENDPOINTS, evaluate,
                        init_csrf_guard)

RESHIP_API = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "reship_api.py")


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

    @bp.route("/ops/api/shipments/<order_id>/reverse-pickup", methods=["POST"])
    def ops_reverse_pickup(order_id):
        return jsonify(reached=True), 401

    @bp.route("/razorpay/verify", methods=["POST"])
    def razorpay_verify():
        return jsonify(reached=True), 200

    pricing_bp = Blueprint("pricing", __name__)

    @pricing_bp.route("/api/lens-pricing/update", methods=["POST"])
    def api_pricing_update():
        return jsonify(reached=True), 401

    @pricing_bp.route("/api/lens-pricing/bulk", methods=["POST"])
    def api_pricing_bulk_update():
        return jsonify(reached=True), 401

    app.register_blueprint(bp)
    app.register_blueprint(pricing_bp)
    init_csrf_guard(app)
    return app


def _app_with_real_pricing():
    app = Flask(__name__)
    app.config.update(CSRF_ENFORCE=True, TRUSTED_HOSTS=["optiwar.com"])
    app.register_blueprint(pricing.bp)
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

    def test_an_ops_bearer_call_with_no_origin_reaches_the_view(self):
        # Ops posts from its own server with the shared token; the view does
        # the authentication, so a 401 here means "reached".
        with _app().test_client() as c:
            r = c.post("/ops/api/shipments/X/reverse-pickup", data="{}",
                       content_type="application/json", base_url=BASE,
                       headers={"Authorization": "Bearer t"})
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.get_json()["reached"])

    def test_lens_pricing_calls_with_no_origin_reach_the_views(self):
        self.assertIn("pricing.api_pricing_update", CSRF_EXEMPT_ENDPOINTS)
        self.assertIn("pricing.api_pricing_bulk_update", CSRF_EXEMPT_ENDPOINTS)
        routes = (
            "/api/lens-pricing/update",
            "/api/lens-pricing/bulk",
        )
        with _app().test_client() as c:
            for route in routes:
                with self.subTest(route=route):
                    r = c.post(route, data={"price": "1"}, base_url=BASE)
                    self.assertEqual(r.status_code, 401)
                    self.assertTrue(r.get_json()["reached"])

    def test_real_pricing_blueprint_without_origin_still_checks_api_key(self):
        with mock.patch.object(pricing, "API_SECRET", "s"):
            with _app_with_real_pricing().test_client() as c:
                r = c.post(
                    "/api/lens-pricing/update",
                    data={"context": "default", "price": "1"},
                    headers={"X-API-Key": "wrong"},
                    base_url=BASE,
                )
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json(), {"error": "Unauthorized"})

    def test_the_same_ops_route_without_a_bearer_is_still_guarded(self):
        # The admin session cookie also authorises it, so a cross-site form
        # post (which cannot carry an Authorization header) must stay blocked.
        with _app().test_client() as c:
            r = c.post("/ops/api/shipments/X/reverse-pickup", data="{}",
                       content_type="application/json", base_url=BASE,
                       headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("main.ops_reverse_pickup", CSRF_EXEMPT_ENDPOINTS)

    def test_every_ops_api_post_route_accepts_a_bearer_call_with_no_origin(self):
        # Ops calls every one of these from its own server, so a route missing
        # from the list is refused with a 403 before its view authenticates.
        with open(RESHIP_API) as f:
            source = f.read()
        routes = re.findall(r'@bp\.route\("(/ops/api/[^"]+)", methods=\["POST"\]\)\s*\n\s*def (\w+)',
                            source)
        self.assertGreaterEqual(len(routes), 11)
        missing = sorted(path for path, view in routes
                         if "main.%s" % view not in BEARER_EXEMPT_ENDPOINTS)
        self.assertEqual(missing, [])

    def test_rx_document_review_and_reparse_are_bearer_exempt(self):
        self.assertIn("main.order_rx_review", BEARER_EXEMPT_ENDPOINTS)
        self.assertIn("main.order_rx_reparse", BEARER_EXEMPT_ENDPOINTS)

    def test_the_guard_still_rejects_a_missing_origin_in_general(self):
        # The exemption is per-endpoint, not a weakening of the decision.
        self.assertEqual(evaluate(None, None, "https://optiwar.com/",
                                  ["optiwar.com"]),
                         ("missing-origin-referer", True))


if __name__ == "__main__":
    unittest.main()
