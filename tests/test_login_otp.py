"""Temporary-password enforcement and gated WhatsApp login OTP tests."""

import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import Blueprint, Flask, jsonify
from werkzeug.security import generate_password_hash, check_password_hash


ROOT = Path(__file__).resolve().parents[1]
_db_for_auth = None
_sent_codes = []


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rows = []
        self.rowcount = 0
        self.lastrowid = None

    def execute(self, query, params=None):
        sql = " ".join(query.split())
        params = tuple(params or ())
        self.rows = []
        self.rowcount = 0
        if sql.startswith("CREATE TABLE"):
            return
        if sql.startswith("SELECT customer_id") and "FROM customers WHERE customer_email = %s" in sql:
            self.rows = [row for row in self.db.customers.values()
                         if row["customer_email"].lower() == str(params[0]).lower()]
            return
        if sql.startswith("SELECT customer_id") and "FROM customers WHERE customer_id=%s" in sql:
            row = self.db.customers.get(int(params[0]))
            self.rows = [row] if row else []
            return
        if sql.startswith("SELECT customer_id") and "FROM customers WHERE customer_email=%s" in sql:
            self.rows = [row for row in self.db.customers.values()
                         if row["customer_email"].lower() == str(params[0]).lower()]
            return
        if sql.startswith("SELECT customer_id") and "FROM customers WHERE customer_id = %s" in sql:
            row = self.db.customers.get(int(params[0]))
            self.rows = [row] if row else []
            return
        if sql.startswith("SELECT expires_at FROM customer_temp_passwords"):
            row = self.db.temp_passwords.get(int(params[0]))
            self.rows = [{"expires_at": row["expires_at"]}] if row else []
            return
        if sql.startswith("SELECT COUNT(*) AS n FROM customer_login_otps"):
            value = params[0]
            if "WHERE phone=%s" in sql:
                self.rows = [{"n": sum(1 for row in self.db.otps.values()
                                       if row["phone"] == value and
                                       row["created_at"] >= datetime.now() - timedelta(minutes=15))}]
            else:
                self.rows = [{"n": sum(1 for row in self.db.otps.values()
                                       if row["created_ip"] == value and
                                       row["created_at"] >= datetime.now() - timedelta(minutes=15))}]
            return
        if sql.startswith("INSERT INTO customer_login_otps"):
            customer_id, phone, code_hash, expires_at, created_ip = params
            otp_id = self.db.next_otp_id
            self.db.next_otp_id += 1
            self.db.otps[otp_id] = {
                "id": otp_id, "customer_id": int(customer_id), "phone": phone,
                "code_hash": code_hash, "expires_at": expires_at,
                "attempts": 0, "used_at": None, "created_ip": created_ip,
                "created_at": datetime.now(),
            }
            self.lastrowid = otp_id
            self.rowcount = 1
            return
        if sql.startswith("UPDATE customer_login_otps SET used_at=NOW() WHERE id=%s"):
            row = self.db.otps.get(int(params[0]))
            if row:
                row["used_at"] = datetime.now()
                self.rowcount = 1
            return
        if sql.startswith("SELECT * FROM customer_login_otps WHERE id=%s"):
            row = self.db.otps.get(int(params[0]))
            self.rows = [row] if row else []
            return
        if sql.startswith("UPDATE customer_login_otps SET attempts="):
            attempts, kill_after, otp_id = params
            row = self.db.otps.get(int(otp_id))
            if row and row["used_at"] is None:
                row["attempts"] = int(attempts)
                if int(kill_after) >= 5:
                    row["used_at"] = datetime.now()
                self.rowcount = 1
            return
        if sql.startswith("UPDATE customer_login_otps SET used_at=NOW() WHERE id=%s AND used_at IS NULL"):
            row = self.db.otps.get(int(params[0]))
            if row and row["used_at"] is None:
                row["used_at"] = datetime.now()
                self.rowcount = 1
            return
        if sql.startswith("UPDATE customers SET customer_password=%s WHERE customer_id=%s"):
            password_hash, customer_id = params
            if int(customer_id) in self.db.customers:
                self.db.customers[int(customer_id)]["customer_password"] = password_hash
                self.rowcount = 1
            return
        if sql.startswith("UPDATE customers SET customer_password = %s WHERE customer_email = %s"):
            password_hash, email = params
            for row in self.db.customers.values():
                if row["customer_email"].lower() == str(email).lower():
                    row["customer_password"] = password_hash
                    self.rowcount += 1
            return
        if sql.startswith("DELETE FROM customer_temp_passwords WHERE customer_id=%s"):
            if int(params[0]) in self.db.temp_passwords:
                del self.db.temp_passwords[int(params[0])]
                self.rowcount = 1
            return
        raise AssertionError("Unexpected SQL: %s" % sql)

    def fetchone(self):
        return dict(self.rows[0]) if self.rows else None

    def fetchall(self):
        return [dict(row) for row in self.rows]


class FakeDB:
    def __init__(self):
        self.customers = {}
        self.temp_passwords = {}
        self.otps = {}
        self.next_otp_id = 1

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class CaptchaGenerator:
    def generate_captcha(self):
        return "ABCD"

    def generate_captcha_image(self, _text):
        return "cG5n"


def _test_auth():
    global _db_for_auth
    package_name = "login_otp_test_pkg"
    if package_name in sys.modules:
        return sys.modules[package_name + ".auth"]
    pkg = types.ModuleType(package_name)
    pkg.__path__ = [str(ROOT)]
    sys.modules[package_name] = pkg

    captcha = types.ModuleType(package_name + ".captcha")
    captcha.CaptchaGenerator = CaptchaGenerator
    sys.modules[captcha.__name__] = captcha

    database = types.ModuleType(package_name + ".db")
    database.get_db = lambda: _db_for_auth
    sys.modules[database.__name__] = database

    cart = types.ModuleType(package_name + ".cart_persist")
    cart.load_cart_from_db = lambda: None
    cart.save_cart_to_db = lambda: None
    sys.modules[cart.__name__] = cart

    identity = types.ModuleType(package_name + ".test_identity")
    identity.is_test_customer = lambda _cursor, _customer_id: False
    sys.modules[identity.__name__] = identity

    ops_customers = types.ModuleType(package_name + ".ops_customers")

    class CustomerError(ValueError):
        pass

    def normalize_phone(value):
        digits = "".join(ch for ch in str(value or "") if ch.isdigit())
        if digits.startswith("00"):
            digits = digits[2:]
        if len(digits) == 10:
            digits = "91" + digits
        if len(digits) < 10:
            raise CustomerError("bad_phone")
        return "+" + digits

    def find_customers(db, email=None, phone=None):
        key = "".join(ch for ch in str(phone or "") if ch.isdigit())[-10:]
        return [
            {"customer_id": row["customer_id"], "name": row["customer_name"],
             "email": row["customer_email"], "phone": row["customer_phone"],
             "date_created": ""}
            for row in db.customers.values()
            if key and "".join(ch for ch in row["customer_phone"] if ch.isdigit())[-10:] == key
        ]

    ops_customers.ensure_schema = lambda _db: None
    ops_customers.normalize_phone = normalize_phone
    ops_customers.find_customers = find_customers
    ops_customers.CustomerError = CustomerError
    ops_customers.OTP_GENERIC_MESSAGE = (
        "We couldn't send a code to this number. Please log in with your email.")
    sys.modules[ops_customers.__name__] = ops_customers

    notifications = types.ModuleType(package_name + ".notifications")

    def send_whatsapp_tracked(phone, template, components):
        _sent_codes.append((phone, template, components))
        return {"ok": True, "request_id": "wa-otp"}

    notifications.send_whatsapp_tracked = send_whatsapp_tracked
    sys.modules[notifications.__name__] = notifications

    spec = importlib.util.spec_from_file_location(
        package_name + ".auth", ROOT / "auth.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LoginOtpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auth = _test_auth()

    def setUp(self):
        global _db_for_auth
        self.db = FakeDB()
        _db_for_auth = self.db
        _sent_codes.clear()
        self.auth._SCHEMA_READY = False
        self.app = Flask(
            __name__, template_folder=str(ROOT / "templates"),
            static_folder=str(ROOT / "static"))
        self.app.secret_key = "test-login-otp-secret"
        self.app.config.update(
            SECRET_KEY="test-login-otp-secret",
            MSG91_LOGIN_OTP_WA_TEMPLATE="ow_login_otp",
        )
        self.app.jinja_env.globals["_"] = lambda value: value
        self.app.session_interface.regenerate = lambda _session: None
        main = Blueprint("main", __name__)

        @main.route("/")
        def index():
            return "home"

        @main.route("/set-password", methods=["GET", "POST"])
        def set_password():
            return self.auth.set_password()

        @main.route("/login/whatsapp/request", methods=["POST"])
        def request_otp():
            return self.auth.request_whatsapp_otp()

        @main.route("/login/whatsapp/verify", methods=["POST"])
        def verify_otp():
            return self.auth.verify_whatsapp_otp()

        @main.route("/login/whatsapp/restart", methods=["POST"])
        def restart_otp():
            return self.auth.restart_whatsapp_otp()

        @main.route("/json")
        def json_route():
            return jsonify({"ok": True})

        self.app.register_blueprint(main)
        self.app.register_blueprint(self.auth.bp)
        self.client = self.app.test_client()

    def add_customer(self, customer_id=1, phone="+919876543210",
                     email="customer@example.com", password="password123"):
        self.db.customers[customer_id] = {
            "customer_id": customer_id, "customer_name": "Test Customer",
            "customer_email": email, "customer_phone": phone,
            "customer_password": generate_password_hash(password),
        }

    def set_captcha(self):
        with self.client.session_transaction(base_url="https://optiwar.com") as session:
            session["captcha"] = "ABCD"

    def request_code(self, phone="9876543210"):
        self.set_captcha()
        return self.client.post(
            "/login/whatsapp/request",
            data={"phone": phone, "captcha": "ABCD"},
            base_url="https://optiwar.com")

    def current_code(self):
        return _sent_codes[-1][2]["body_1"]["value"]

    def test_otp_routes_return_404_and_login_hides_option_when_disabled(self):
        self.app.config["MSG91_LOGIN_OTP_WA_TEMPLATE"] = ""
        with patch.object(self.auth, "render_template", return_value="login page") as render:
            response = self.client.get("/auth/login", base_url="https://optiwar.com")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(render.call_args.kwargs["otp_enabled"])
        disabled = self.client.post(
            "/login/whatsapp/request", data={"phone": "9876543210"},
            base_url="https://optiwar.com")
        self.assertEqual(disabled.status_code, 404)

    def test_restart_clears_pending_and_shows_request_form_again(self):
        self.add_customer()
        self.request_code()
        otp_id = next(iter(self.db.otps))
        with self.client.session_transaction(base_url="https://optiwar.com") as session:
            self.assertEqual(session["_wa_login_otp_id"], otp_id)
        response = self.client.post(
            "/login/whatsapp/restart", base_url="https://optiwar.com")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/auth/login")
        self.assertIsNone(self.db.otps[otp_id]["used_at"])
        with self.client.session_transaction(base_url="https://optiwar.com") as session:
            self.assertNotIn("_wa_login_otp_id", session)
        with patch.object(self.auth, "render_template", return_value="login page") as render:
            self.client.get("/auth/login", base_url="https://optiwar.com")
        self.assertFalse(render.call_args.kwargs["otp_pending"])

    def test_restart_returns_404_when_otp_is_disabled(self):
        self.app.config["MSG91_LOGIN_OTP_WA_TEMPLATE"] = ""
        with self.client.session_transaction(base_url="https://optiwar.com") as session:
            session["_wa_login_otp_id"] = 12
        response = self.client.post(
            "/login/whatsapp/restart", base_url="https://optiwar.com")
        self.assertEqual(response.status_code, 404)

    def test_restart_does_not_reset_otp_rate_limit(self):
        self.add_customer()
        for _ in range(3):
            self.request_code()
        self.assertEqual(len(self.db.otps), 3)
        response = self.client.post(
            "/login/whatsapp/restart", base_url="https://optiwar.com")
        self.assertEqual(response.status_code, 302)
        self.request_code()
        self.assertEqual(len(_sent_codes), 3)
        self.assertEqual(len(self.db.otps), 3)

    def test_ambiguous_phone_gets_generic_reply_and_sends_nothing(self):
        self.add_customer(1)
        self.add_customer(2, phone="+91 98765-43210",
                          email="other@example.com")
        response = self.request_code()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(_sent_codes, [])
        with self.client.session_transaction(base_url="https://optiwar.com") as session:
            self.assertNotIn("_wa_login_otp_id", session)
            self.assertIn(
                "We couldn't send a code to this number. Please log in with your email.",
                [message for _category, message in session.get("_flashes", [])])

    def test_request_is_limited_to_three_codes_per_phone_and_ip(self):
        self.add_customer()
        for _ in range(3):
            self.request_code()
        self.assertEqual(len(_sent_codes), 3)
        self.request_code()
        self.assertEqual(len(_sent_codes), 3)
        self.assertEqual(len(self.db.otps), 3)

    def test_five_wrong_attempts_kill_the_code(self):
        self.add_customer()
        self.request_code()
        otp_id = next(iter(self.db.otps))
        code = self.current_code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.client.post("/login/whatsapp/verify", data={"code": wrong},
                             base_url="https://optiwar.com")
        self.assertEqual(self.db.otps[otp_id]["attempts"], 5)
        self.assertIsNotNone(self.db.otps[otp_id]["used_at"])
        with self.client.session_transaction(base_url="https://optiwar.com") as session:
            self.assertNotIn("_wa_login_otp_id", session)

    def test_otp_is_single_use_and_success_logs_in(self):
        self.add_customer()
        self.request_code()
        otp_id = next(iter(self.db.otps))
        code = self.current_code()
        with patch.object(self.app.logger, "info") as log:
            response = self.client.post(
                "/login/whatsapp/verify", data={"code": code},
                base_url="https://optiwar.com")
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(self.db.otps[otp_id]["used_at"])
        self.assertTrue(any("ACTIVITY:LOGIN_WA_OTP" in str(call)
                            for call in log.call_args_list))
        with self.client.session_transaction(base_url="https://optiwar.com") as session:
            self.assertEqual(session["user_id"], 1)
            self.assertNotIn("must_set_password", session)
            self.assertNotIn("_wa_login_otp_id", session)
        self.client.post("/login/whatsapp/verify", data={"code": code},
                         base_url="https://optiwar.com")
        self.assertEqual(self.db.otps[otp_id]["attempts"], 0)

    def test_expired_temp_password_is_refused(self):
        self.add_customer()
        self.db.temp_passwords[1] = {
            "customer_id": 1, "expires_at": datetime.now() - timedelta(seconds=1),
        }
        self.set_captcha()
        response = self.client.post(
            "/auth/login",
            data={"email": "customer@example.com", "password": "password123",
                  "captcha": "ABCD"},
            base_url="https://optiwar.com")
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction(base_url="https://optiwar.com") as session:
            self.assertNotIn("user_id", session)
            self.assertIn(
                "Your temporary password has expired. Log in with a WhatsApp code or use Forgot password.",
                [message for _category, message in session.get("_flashes", [])])

    def test_valid_temp_password_requires_set_password_and_cleans_row(self):
        self.add_customer()
        self.db.temp_passwords[1] = {
            "customer_id": 1, "expires_at": datetime.now() + timedelta(days=1),
        }
        self.set_captcha()
        response = self.client.post(
            "/auth/login",
            data={"email": "customer@example.com", "password": "password123",
                  "captcha": "ABCD"},
            base_url="https://optiwar.com")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/set-password")
        with self.client.session_transaction(base_url="https://optiwar.com") as session:
            self.assertTrue(session["must_set_password"])
        blocked = self.client.get("/json", base_url="https://optiwar.com",
                                  headers={"Accept": "application/json"})
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.get_json(), {"error": "password_change_required"})
        response = self.client.post(
            "/set-password",
            data={"password": "new-password-123", "confirm_password": "new-password-123"},
            base_url="https://optiwar.com")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(1, self.db.temp_passwords)
        self.assertTrue(check_password_hash(
            self.db.customers[1]["customer_password"], "new-password-123"))
        with self.client.session_transaction(base_url="https://optiwar.com") as session:
            self.assertNotIn("must_set_password", session)

    def test_forgot_password_reset_deletes_temp_password_record(self):
        self.add_customer()
        self.db.temp_passwords[1] = {
            "customer_id": 1, "expires_at": datetime.now() + timedelta(days=1),
        }
        with self.client.session_transaction(base_url="https://optiwar.com") as session:
            session["_reset_verified"] = True
            session["_reset_email"] = "customer@example.com"
        response = self.client.post(
            "/auth/reset-password",
            data={"password": "forgot-reset", "confirm_password": "forgot-reset"},
            base_url="https://optiwar.com")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(1, self.db.temp_passwords)
        self.assertTrue(check_password_hash(
            self.db.customers[1]["customer_password"], "forgot-reset"))


if __name__ == "__main__":
    unittest.main()
