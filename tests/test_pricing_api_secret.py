import importlib.util
import os
import unittest
from unittest import mock

from flask import Flask

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("pricing_t", os.path.join(REPO, "pricing.py"))
pricing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pricing)


def _client():
    app = Flask(__name__)
    app.register_blueprint(pricing.bp)
    return app.test_client()


class PricingApiSecretTest(unittest.TestCase):
    def test_an_unset_secret_refuses_every_update(self):
        with mock.patch.object(pricing, "API_SECRET", ""):
            r = _client().post("/api/lens-pricing/update", data={"context": "default"})
        self.assertEqual(r.status_code, 401)

    def test_a_wrong_key_is_refused_and_the_right_one_reaches_the_view(self):
        with mock.patch.object(pricing, "API_SECRET", "s3"), \
                mock.patch.object(pricing, "load_pricing", return_value={}):
            c = _client()
            self.assertEqual(c.post("/api/lens-pricing/update",
                                    headers={"X-API-Key": "nope"}).status_code, 401)
            self.assertNotEqual(c.post("/api/lens-pricing/update",
                                       headers={"X-API-Key": "s3"},
                                       data={"price": "1"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
