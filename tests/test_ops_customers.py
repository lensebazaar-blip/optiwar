"""Ops customer creation and Self-profile scan-request coverage."""

import hashlib
import importlib.util
import os
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import Blueprint, Flask, request
from werkzeug.security import check_password_hash


ROOT = Path(__file__).resolve().parents[1]


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
        if sql.startswith("SELECT customer_id") and "FROM customers WHERE customer_id=%s" in sql:
            customer = self.db.customers.get(int(params[0]))
            self.rows = [customer] if customer else []
            return
        if sql.startswith("SELECT customer_id") and "FROM customers WHERE" in sql:
            email = None
            phone = None
            offset = 0
            if "LOWER(customer_email)=%s" in sql:
                email = str(params[offset]).lower()
                offset += 1
            if "RIGHT(REGEXP_REPLACE" in sql:
                phone = str(params[offset])[-10:]
            self.rows = [
                row for row in self.db.customers.values()
                if ((email and row["customer_email"].lower() == email)
                    or (phone and "".join(filter(str.isdigit,
                                                  row["customer_phone"]))[-10:] == phone))
            ]
            self.rows.sort(key=lambda row: row["customer_id"], reverse=True)
            return
        if sql.startswith("INSERT INTO customers"):
            cid = self.db.next_customer_id
            self.db.next_customer_id += 1
            name, email, password_hash, phone = params
            self.db.customers[cid] = {
                "customer_id": cid, "customer_name": name,
                "customer_email": email, "customer_password": password_hash,
                "customer_phone": phone, "date_created": datetime(2026, 1, 2),
            }
            self.lastrowid = cid
            self.rowcount = 1
            return
        if sql.startswith("INSERT INTO customer_temp_passwords"):
            cid, expires, created_by = params
            self.db.temp_passwords[cid] = {
                "customer_id": cid, "expires_at": expires,
                "created_by": created_by, "created_at": datetime.now(),
            }
            self.rowcount = 1
            return
        if sql.startswith("SELECT COUNT(*) AS n FROM face_scan_invites"):
            customer_id, sender = params
            self.rows = [{"n": sum(
                1 for row in self.db.invites.values()
                if row["customer_id"] == int(customer_id)
                and row["sender_name"] == sender
                and row["created_at"] > datetime.now() - timedelta(days=1))}]
            return
        if sql.startswith("UPDATE face_scan_invites SET status=%s"):
            status, cancelled_at, profile_id = params
            for row in self.db.invites.values():
                if row["face_profile_id"] == int(profile_id) and row["active_slot"] == 1:
                    row["status"] = status
                    row["cancelled_at"] = cancelled_at
                    row["active_slot"] = None
                    self.rowcount += 1
            return
        if sql.startswith("INSERT INTO face_scan_invites"):
            (request_uuid, customer_id, profile_id, channel, email, phone,
             token_hash, status, sender_name, site_host, expires_at,
             created_ip) = params
            invite_id = self.db.next_invite_id
            self.db.next_invite_id += 1
            row = {
                "id": invite_id, "request_uuid": request_uuid,
                "customer_id": customer_id, "face_profile_id": profile_id,
                "channel": channel, "recipient_email": email,
                "recipient_phone": phone, "token_hash": token_hash,
                "status": status, "active_slot": 1,
                "sender_name": sender_name, "site_host": site_host,
                "delivery_status": "NOT_SENT", "delivery_error": None,
                "delivery_ref": None, "delivered_at": None,
                "delivery_attempts": 0, "expires_at": expires_at,
                "opened_at": None, "completed_at": None,
                "created_at": datetime.now(), "cancelled_at": None,
                "scan_group_id": None,
            }
            self.db.invites[request_uuid] = row
            self.lastrowid = invite_id
            self.rowcount = 1
            return
        if sql.startswith("SELECT * FROM face_scan_invites WHERE request_uuid=%s"):
            row = self.db.invites.get(str(params[0]))
            self.rows = [row] if row else []
            return
        if sql.startswith("INSERT IGNORE INTO face_events"):
            self.db.events.append(params)
            self.rowcount = 1
            return
        if sql.startswith("UPDATE face_scan_invites SET delivery_status="):
            status, ref, error, delivered_at, invite_id = params
            row = next((item for item in self.db.invites.values()
                        if item["id"] == int(invite_id)), None)
            if row:
                row.update(delivery_status=status, delivery_ref=ref,
                           delivery_error=error, delivered_at=delivered_at)
                row["delivery_attempts"] += 1
                self.rowcount = 1
            return
        raise AssertionError("Unexpected query: %s" % sql)

    def fetchone(self):
        return dict(self.rows[0]) if self.rows else None

    def fetchall(self):
        return [dict(row) for row in self.rows]


class FakeDB:
    def __init__(self):
        self.customers = {}
        self.temp_passwords = {}
        self.profiles = {}
        self.invites = {}
        self.events = []
        self.next_customer_id = 1
        self.next_invite_id = 1
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def begin(self):
        pass

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _test_modules():
    package_name = "ops_customer_test_pkg"
    if package_name in sys.modules:
        return tuple(sys.modules[package_name + "." + name] for name in
                     ("ops_customers", "ops_customers_api", "face_scan_invites"))
    pkg = types.ModuleType(package_name)
    pkg.__path__ = [str(ROOT)]
    sys.modules[package_name] = pkg

    profiles = types.ModuleType(package_name + ".face_profiles")

    def ensure_schema(_db):
        return None

    def ensure_self(db, customer_id, account_name=None):
        profile = db.profiles.setdefault(int(customer_id), {
            "id": 100 + int(customer_id), "customer_id": int(customer_id),
            "display_name": account_name or "Self", "is_self": 1,
        })
        return dict(profile)

    profiles.ensure_schema = ensure_schema
    class ProfileError(Exception):
        def __init__(self, code, message, status=400):
            super().__init__(message)
            self.code = code
            self.status = status

    profiles.ProfileError = ProfileError
    profiles.ensure_self = ensure_self
    profiles.require_profile = lambda _db, cid, pid: {
        "id": int(pid), "customer_id": int(cid), "is_self": 1,
    }
    profiles._jsonable = lambda value: value.isoformat() if hasattr(value, "isoformat") else value
    sys.modules[profiles.__name__] = profiles

    notifications = types.ModuleType(package_name + ".notifications")
    notifications.send_whatsapp_tracked = lambda *_args, **_kwargs: {
        "ok": False, "error": "not_stubbed", "request_id": ""}
    sys.modules[notifications.__name__] = notifications

    db_module = types.ModuleType(package_name + ".db")
    db_module.get_db = lambda: None
    sys.modules[db_module.__name__] = db_module
    ops_auth = types.ModuleType(package_name + ".order_face_scan")
    ops_auth._ops_auth = lambda: request.headers.get("Authorization") == "Bearer good"
    sys.modules[ops_auth.__name__] = ops_auth

    def load(name, path):
        spec = importlib.util.spec_from_file_location(
            package_name + "." + name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    fsi = load("face_scan_invites", ROOT / "face_scan_invites.py")
    ops_customers = load("ops_customers", ROOT / "ops_customers.py")
    ops_api = load("ops_customers_api", ROOT / "ops_customers_api.py")
    return ops_customers, ops_api, fsi


class OpsCustomersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ops, cls.api, cls.fsi = _test_modules()

    def setUp(self):
        self.db = FakeDB()
        self.ops._SCHEMA_READY = False
        self.fsi._SCHEMA_READY = False

    def test_duplicate_email_returns_matches_without_insert(self):
        self.db.customers[7] = {
            "customer_id": 7, "customer_name": "Existing",
            "customer_email": "customer@example.com",
            "customer_password": "hash", "customer_phone": "+919876543210",
            "date_created": datetime(2025, 1, 1),
        }
        with self.assertRaises(self.ops.CustomerExists) as raised:
            self.ops.create_customer(
                self.db, "New Person", "CUSTOMER@example.com", "9876543211",
                mailer=lambda *_: self.fail("duplicate must not send"))
        self.assertEqual(raised.exception.matches[0]["customer_id"], 7)
        self.assertEqual(len(self.db.customers), 1)
        self.assertEqual(self.db.temp_passwords, {})

    def test_duplicate_phone_matches_last_ten_digits(self):
        self.db.customers[8] = {
            "customer_id": 8, "customer_name": "Existing",
            "customer_email": "old@example.com",
            "customer_password": "hash", "customer_phone": "+91 (987) 654-3210",
            "date_created": datetime(2025, 1, 1),
        }
        with self.assertRaises(self.ops.CustomerExists) as raised:
            self.ops.create_customer(
                self.db, "New Person", "new@example.com", "9876543210",
                mailer=lambda *_: self.fail("duplicate must not send"))
        self.assertEqual(raised.exception.matches[0]["customer_id"], 8)
        self.assertEqual(len(self.db.customers), 1)

    def test_create_hashes_password_sets_seven_day_expiry_and_self_profile(self):
        fixed_now = datetime(2026, 3, 1, 12, 0)
        sent = []
        result = self.ops.create_customer(
            self.db, "New Customer", "New@Example.com", "9876543210",
            created_by="ops-user", otp_enabled=False, now=fixed_now,
            mailer=lambda to, subject, html_body, text:
                sent.append((to, subject, html_body, text)))
        self.assertEqual(result["customer_id"], 1)
        self.assertEqual(set(result), {"customer_id", "welcome_email"})
        self.assertNotIn("password", str(result).lower())
        customer = self.db.customers[1]
        password = sent[0][3].split("Temporary password: ", 1)[1].splitlines()[0]
        self.assertEqual(len(password), 10)
        self.assertTrue(set(password).issubset(set(self.ops.TEMP_PASSWORD_ALPHABET)))
        self.assertTrue(check_password_hash(customer["customer_password"], password))
        self.assertEqual(customer["customer_email"], "new@example.com")
        self.assertEqual(customer["customer_phone"], "+919876543210")
        self.assertEqual(self.db.temp_passwords[1]["expires_at"],
                         fixed_now + timedelta(days=7))
        self.assertEqual(self.db.temp_passwords[1]["created_by"], "ops-user")
        self.assertEqual(self.db.profiles[1]["display_name"], "New Customer")
        self.assertTrue(result["welcome_email"]["ok"])

    def test_welcome_preview_and_email_omit_whatsapp_while_otp_is_disabled(self):
        self.assertEqual(self.ops.WELCOME_LOGIN_URL,
                         "https://optiwar.com/auth/login")
        preview = self.ops.message_preview(
            "welcome", "A Customer", "a@example.com", "+919876543082",
            otp_enabled=False)
        self.assertEqual(preview["subject"], "Welcome to Optiwar \u2013 your login details")
        self.assertIn(
            "Temporary password: \u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 "
            "(generated when you press Send)", preview["text"])
        self.assertNotIn("one-time code on WhatsApp", preview["text"])

        captured = []
        self.ops._send_welcome_email(
            "a@example.com", "A Customer", "+919876543082", "abcdefgh23",
            False, mailer=lambda *args: captured.append(args))
        to, subject, body_html, body_text = captured[0]
        self.assertEqual((to, subject),
                         ("a@example.com", "Welcome to Optiwar \u2013 your login details"))
        self.assertIn("OPTIWAR", body_html)
        self.assertIn("background:#5b3df5", body_html)
        self.assertIn("This password works for 7 days.", body_text)
        self.assertNotIn("one-time code on WhatsApp", body_text)

    def test_ops_face_scan_uses_self_and_stored_contacts_and_replaces_active_link(self):
        self.db.customers[3] = {
            "customer_id": 3, "customer_name": "Scan Owner",
            "customer_email": "owner@example.com",
            "customer_password": "hash", "customer_phone": "+919876543210",
            "date_created": datetime(2025, 1, 1),
        }
        emails = []
        whatsapp = []

        def mailer(to, subject, html_body, text, sender=None):
            emails.append((to, subject, html_body, text, sender))

        def send_wa(to, template, components):
            whatsapp.append((to, template, components))
            return {"ok": True, "request_id": "wa-123"}

        now = datetime(2026, 3, 1, 12, 0)
        first = self.ops.request_face_scan(
            self.db, 3, ["email", "whatsapp"], created_by="ops-user",
            mailer=mailer, whatsapp=send_wa, now=now,
            environ={"FACE_SCAN_REQUEST_WA_TEMPLATE": "face_scan_request_v2"})
        second = self.ops.request_face_scan(
            self.db, 3, ["email", "whatsapp"], created_by="ops-user",
            mailer=mailer, whatsapp=send_wa, now=now + timedelta(minutes=1),
            environ={"FACE_SCAN_REQUEST_WA_TEMPLATE": "face_scan_request_v2"})
        invites = list(self.db.invites.values())
        self.assertEqual(len(invites), 2)
        self.assertEqual(invites[0]["status"], self.fsi.ST_CANCELLED)
        self.assertIsNone(invites[0]["active_slot"])
        self.assertEqual(invites[1]["active_slot"], 1)
        self.assertEqual(invites[1]["face_profile_id"], 103)
        self.assertEqual(invites[1]["recipient_email"], "owner@example.com")
        self.assertEqual(invites[1]["recipient_phone"], "+919876543210")
        self.assertEqual(invites[1]["channel"], "whatsapp")
        self.assertEqual(emails[-1][0], "owner@example.com")
        self.assertEqual(whatsapp[-1][0], "919876543210")
        self.assertEqual(whatsapp[-1][1], "face_scan_request_v2")
        email_link = emails[-1][3].split("Complete Face Scan: ", 1)[1].splitlines()[0]
        self.assertEqual(whatsapp[-1][2]["body_2"]["value"], email_link)
        self.assertEqual(first["deliveries"]["whatsapp"]["request_id"], "wa-123")
        self.assertTrue(second["deliveries"]["email"]["ok"])
        self.assertEqual(invites[-1]["delivery_status"], self.fsi.DL_SENT)
        self.assertEqual(len(self.db.events), 4)

    def test_face_scan_missing_stored_contact_is_bad_request(self):
        self.db.customers[4] = {
            "customer_id": 4, "customer_name": "No WhatsApp",
            "customer_email": "customer@example.com",
            "customer_password": "hash", "customer_phone": "",
            "date_created": datetime(2025, 1, 1),
        }
        with self.assertRaises(self.ops.CustomerError) as raised:
            self.ops.request_face_scan(self.db, 4, ["whatsapp"])
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(self.db.invites, {})

    def test_face_scan_ops_requests_are_capped_at_five_per_day(self):
        self.db.customers[5] = {
            "customer_id": 5, "customer_name": "Rate Limited",
            "customer_email": "customer@example.com",
            "customer_password": "hash", "customer_phone": "+919876543210",
            "date_created": datetime(2025, 1, 1),
        }
        for attempt in range(5):
            self.ops.request_face_scan(
                self.db, 5, ["email"], mailer=lambda *_a, **_k: None,
                now=datetime.now() + timedelta(minutes=attempt))
        with self.assertRaises(self.ops.CustomerError) as raised:
            self.ops.request_face_scan(
                self.db, 5, ["email"], mailer=lambda *_a, **_k: None)
        self.assertEqual(raised.exception.status, 429)

    def test_both_post_routes_require_bearer_auth(self):
        self.ops._SCHEMA_READY = False
        fake_db = self.db
        self.api.__dict__["get_db"] = lambda: fake_db
        bp = Blueprint("main", __name__)
        self.api.register(bp)
        app = Flask(__name__)
        app.secret_key = "test"
        app.config["TRUSTED_HOSTS"] = ["optiwar.com"]
        app.register_blueprint(bp)
        with app.test_client() as client:
            create = client.post("/ops/api/customers", json={},
                                 base_url="https://optiwar.com")
            scan = client.post(
                "/ops/api/customers/1/face-scan-request", json={},
                base_url="https://optiwar.com")
        self.assertEqual(create.status_code, 401)
        self.assertEqual(scan.status_code, 401)
