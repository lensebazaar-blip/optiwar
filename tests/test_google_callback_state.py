import importlib.util
import os
import sys
import types
import unittest
from unittest import mock

from authlib.integrations.base_client.errors import MismatchingStateError
from flask import Blueprint, Flask


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _load_auth():
    package_name = "google_callback_state_test"
    package = types.ModuleType(package_name)
    package.__path__ = [REPO]
    sys.modules[package_name] = package

    def add_stub(name, **attrs):
        module = types.ModuleType(package_name + "." + name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[module.__name__] = module
        setattr(package, name, module)

    add_stub("captcha", CaptchaGenerator=object)
    add_stub("db", get_db=lambda: None)
    add_stub("cart_persist", load_cart_from_db=lambda: None, save_cart_to_db=lambda: None)
    add_stub("test_identity", is_test_customer=lambda *args: False)

    spec = importlib.util.spec_from_file_location(
        package_name + ".auth", os.path.join(REPO, "auth.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    setattr(package, "auth", module)
    spec.loader.exec_module(module)
    return module


class GoogleCallbackState(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.auth = _load_auth()
        cls.app = Flask(__name__)
        cls.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        cls.app.register_blueprint(cls.auth.bp)
        main = Blueprint("main", __name__)
        main.add_url_rule("/", "index", lambda: "home")
        cls.app.register_blueprint(main)
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def _oauth(self, error):
        return types.SimpleNamespace(
            google=types.SimpleNamespace(
                authorize_access_token=mock.Mock(side_effect=error)
            )
        )

    def test_signed_in_stale_callback_uses_safe_next_without_error_or_flash(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = 42
            sess["_login_next"] = "/cart"
        with mock.patch.object(self.auth, "oauth", self._oauth(MismatchingStateError())):
            with self.assertLogs(self.app.logger, level="INFO") as captured:
                response = self.client.get("/auth/google/callback")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/cart")
        self.assertTrue(any(
            "Google callback repeated after sign-in (stale state); user 42" in record
            for record in captured.output
        ))
        self.assertFalse(any("ERROR" in record for record in captured.output))
        with self.client.session_transaction() as sess:
            self.assertNotIn("_flashes", sess)
            self.assertNotIn("_login_next", sess)

    def test_signed_in_stale_callback_defaults_to_main_index(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = 42
            sess["_login_next"] = "https://untrusted.example/next"
        with mock.patch.object(self.auth, "oauth", self._oauth(MismatchingStateError())):
            response = self.client.get("/auth/google/callback")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        with self.client.session_transaction() as sess:
            self.assertNotIn("_flashes", sess)
            self.assertNotIn("_login_next", sess)

    def test_anonymous_stale_callback_warns_and_redirects_to_login(self):
        with mock.patch.object(self.auth, "oauth", self._oauth(MismatchingStateError())):
            with self.assertLogs(self.app.logger, level="WARNING") as captured:
                response = self.client.get("/auth/google/callback")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/auth/login")
        self.assertTrue(any(
            "Google sign-in state mismatch (expired or repeated callback)" in record
            for record in captured.output
        ))
        self.assertFalse(any("ERROR" in record for record in captured.output))
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["_flashes"], [(
                "warning",
                "Your Google sign-in took too long or was opened twice. Please try again."
            )])

    def test_other_oauth_errors_still_use_the_generic_error_handler(self):
        with mock.patch.object(self.auth, "oauth", self._oauth(RuntimeError("provider down"))):
            with self.assertLogs(self.app.logger, level="ERROR") as captured:
                response = self.client.get("/auth/google/callback")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/auth/login")
        self.assertTrue(any("Google OAuth error: provider down" in record
                            for record in captured.output))


if __name__ == "__main__":
    unittest.main()
