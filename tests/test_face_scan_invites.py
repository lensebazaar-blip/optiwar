"""Remote one-time face-scan requests.

The questions that matter: can a stranger with the link reach anything but
the one scan it was made for; does opening, previewing or refreshing the link
spend it; does a cancellation from the owner beat a page the guest already
has open; does the plaintext token ever touch the database; does a completed
request attach its scan to exactly the profile it was bound to; and does a
failed WhatsApp send leave the request intact.

    OPTIWAR_TEST_MYSQL_DB=optiwar2 python3 -m unittest tests.test_face_scan_invites
"""
import importlib.util
import os
import re
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests"))

from test_face_profiles import (AVAILABLE, LEGACY_DDL, C1, C2,  # noqa: E402
                                _connect, _load_service, _wipe)

MEAS = dict(pd_far=61.0, pd_near=58.5, face_width=131.0, eye_mouth=70.0,
            recommended_diameter=49, recommended_bridge=23,
            recommended_length=140, decentration=1.0)

WA_OK = "+919999900001"
EMAIL_OK = "guest@example.com"


def _load_pkg(fp_mod, get_db):
    pkg_name = "fsi_pkg"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [REPO]
    sys.modules[pkg_name] = pkg
    sys.modules[pkg_name + ".face_profiles"] = fp_mod
    db_mod = types.ModuleType(pkg_name + ".db")
    db_mod.get_db = get_db
    sys.modules[pkg_name + ".db"] = db_mod
    out = {}
    for name in ("face_scan_invites", "face_scan_done", "face_profiles_api",
                 "face_scan_invites_api", "csrf_guard"):
        spec = importlib.util.spec_from_file_location(
            pkg_name + "." + name, os.path.join(REPO, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        out[name] = mod
    return out


# Production's customers, reduced to what the completion notice reads.
CUSTOMERS_DDL = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id INT NOT NULL,
    customer_name VARCHAR(120) NULL,
    customer_email VARCHAR(191) NULL,
    customer_phone VARCHAR(32) NULL,
    PRIMARY KEY (customer_id)
) ENGINE=InnoDB
"""


def _seed_customer(db, cid, name, email, phone):
    cur = db.cursor()
    cur.execute(CUSTOMERS_DDL)
    cur.execute("REPLACE INTO customers (customer_id, customer_name, customer_email, "
                "customer_phone) VALUES (%s,%s,%s,%s)", (cid, name, email, phone))
    db.commit()


def _wipe_invites(db, *cids):
    cur = db.cursor()
    cur.execute(CUSTOMERS_DDL)
    for cid in cids:
        cur.execute("DELETE FROM customers WHERE customer_id=%s", (cid,))
        cur.execute("DELETE FROM face_events WHERE customer_id=%s", (cid,))
        cur.execute("DELETE FROM face_scan_invites WHERE customer_id=%s", (cid,))
    db.commit()


@unittest.skipUnless(AVAILABLE, "MariaDB test database not reachable")
class ServiceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fp = _load_service()
        cls.db = _connect()
        cls.db.cursor().execute(LEGACY_DDL)
        cls.db.commit()
        cls.fp.ensure_schema(cls.db)
        cls.fsi = _load_pkg(cls.fp, lambda: cls.db)["face_scan_invites"]
        cls.fsi.ensure_schema(cls.db)

    @classmethod
    def tearDownClass(cls):
        _wipe_invites(cls.db, C1, C2)
        _wipe(cls.db, C1, C2)
        cls.db.close()

    def setUp(self):
        _wipe_invites(self.db, C1, C2)
        _wipe(self.db, C1, C2)
        self.fp.ensure_self(self.db, C1, "Sudhanshu")
        self.wife = self.fp.create_profile(self.db, C1, "Wife", "spouse", consent=True)["id"]

    def _create(self, channel="whatsapp", dest=WA_OK, pid=None, **kw):
        return self.fsi.create(self.db, C1, pid or self.wife, channel, dest,
                               sender_name="Sudhanshu", site_host="optiwar.com", **kw)

    def test_token_is_never_stored_and_hashes_to_the_row(self):
        row, token = self._create()
        self.assertGreaterEqual(len(token), 32)
        self.assertLessEqual(len(self.fsi.link_for(token, "optiwar.com")), 60)
        cur = self.db.cursor()
        cur.execute("SELECT * FROM face_scan_invites WHERE request_uuid=%s", (row["request_uuid"],))
        stored = cur.fetchone()
        self.assertEqual(stored["token_hash"], self.fsi.hash_token(token))
        for v in stored.values():
            self.assertNotEqual(v, token)
        self.assertEqual(self.fsi.by_token(self.db, token)["request_uuid"], row["request_uuid"])
        self.assertIsNone(self.fsi.by_token(self.db, token[:-1] + ("A" if token[-1] != "A" else "B")))
        self.assertIsNone(self.fsi.by_token(self.db, ""))

    def test_expiry_is_24h_and_enforced_server_side(self):
        row, token = self._create()
        ttl = row["expires_at"] - row["created_at"]
        self.assertEqual(ttl, timedelta(hours=24))
        late = row["expires_at"] + timedelta(seconds=1)
        self.assertFalse(self.fsi.is_usable(row, now=late))
        with self.assertRaises(self.fsi.InviteError) as cm:
            self.fsi.complete(self.db, row, MEAS, now=late)
        self.assertEqual(cm.exception.code, "expired")
        self.assertEqual(self.fsi.by_uuid(self.db, row["request_uuid"])["status"], "EXPIRED")

    def test_self_and_foreign_profiles_are_refused(self):
        me = self.fp.ensure_self(self.db, C1, "Sudhanshu")["id"]
        with self.assertRaises(self.fsi.InviteError) as cm:
            self._create(pid=me)
        self.assertEqual(cm.exception.status, 409)
        self.fp.ensure_self(self.db, C2, "Other")
        with self.assertRaises(self.fp.ProfileError) as cm:
            self.fsi.create(self.db, C2, self.wife, "whatsapp", WA_OK)
        self.assertEqual(cm.exception.status, 404)
        self.assertIsNone(self.fsi.for_customer(self.db, C2).get(self.wife))

    def test_owner_contacts_are_refused_however_formatted(self):
        """The account holder's own phone or email is not another person's
        destination: refused after normalisation, nothing created."""
        contacts = {"phones": ["9810113801"], "emails": ["LenseBazaar@gmail.com "]}
        for dest in ("9810113801", "+91 9810113801", "+919810113801", "0091 9810113801",
                     "91-98101-13801"):
            with self.assertRaises(self.fsi.InviteError) as cm:
                self._create(channel="whatsapp", dest=dest, contacts=contacts)
            self.assertEqual((cm.exception.code, cm.exception.status), ("own_phone", 422), dest)
        for dest in ("lensebazaar@gmail.com", " Lensebazaar@Gmail.com"):
            with self.assertRaises(self.fsi.InviteError) as cm:
                self._create(channel="email", dest=dest, contacts=contacts)
            self.assertEqual(cm.exception.code, "own_email", dest)
        self.assertIsNone(self.fsi.for_customer(self.db, C1).get(self.wife))
        # a different number is still fine, and an account without a phone blocks nothing
        self._create(channel="whatsapp", dest="9810113802", contacts=contacts)
        self._create(channel="whatsapp", dest="9810113801",
                     contacts={"phones": [], "emails": ["lensebazaar@gmail.com"]})

    def test_destination_validation(self):
        with self.assertRaises(self.fsi.InviteError):
            self._create(channel="email", dest="not-an-email")
        with self.assertRaises(self.fsi.InviteError):
            self._create(channel="whatsapp", dest="12")
        with self.assertRaises(self.fsi.InviteError):
            self._create(channel="sms", dest=WA_OK)
        row, _ = self._create(channel="whatsapp", dest="98765 43210")
        self.assertEqual(row["recipient_phone"], "+919876543210")

    def test_open_and_refresh_do_not_consume_and_opened_event_is_once(self):
        row, token = self._create()
        r1 = self.fsi.mark_opened(self.db, row)
        r2 = self.fsi.mark_opened(self.db, r1)
        self.assertEqual(r1["status"], "OPENED")
        self.assertEqual(r1["opened_at"], r2["opened_at"])
        self.assertTrue(self.fsi.is_usable(r2))
        cur = self.db.cursor()
        cur.execute("SELECT event_type FROM face_events WHERE request_uuid=%s ORDER BY id",
                    (row["request_uuid"],))
        types_ = [e["event_type"] for e in cur.fetchall()]
        self.assertEqual(types_.count("face.scan_request.opened"), 1)

    def test_completion_binds_the_scan_to_the_profile_and_spends_the_request(self):
        row, token = self._create()
        row = self.fsi.record_consent(self.db, self.fsi.mark_opened(self.db, row))
        sid = self.fsi.complete(self.db, row, MEAS, algorithm_version="tryon-7.4",
                                completed_ip="203.0.113.9")
        prof = self.fp.get_profile(self.db, C1, self.wife)
        self.assertEqual(int(prof["latest_scan_id"]), int(sid))
        cur = self.db.cursor()
        cur.execute("SELECT face_profile_id, source FROM face_scans WHERE id=%s", (sid,))
        s = cur.fetchone()
        self.assertEqual((int(s["face_profile_id"]), s["source"]), (self.wife, "remote_invite"))
        live = self.fsi.by_uuid(self.db, row["request_uuid"])
        self.assertEqual(live["status"], "COMPLETED")
        self.assertEqual(int(live["completed_scan_id"]), int(sid))
        self.assertIsNone(live["active_slot"])
        self.assertFalse(self.fsi.is_usable(live))
        # the Self profile is untouched
        me = self.fp.get_profile(self.db, C1, self.fp.ensure_self(self.db, C1, "Sudhanshu")["id"])
        self.assertIsNone(me["latest_scan_id"])

    def test_second_completion_is_refused_and_leaves_one_scan(self):
        row, token = self._create()
        row = self.fsi.record_consent(self.db, row)
        self.fsi.complete(self.db, row, MEAS)
        with self.assertRaises(self.fsi.InviteError) as cm:
            self.fsi.complete(self.db, row, dict(MEAS, pd_far=70.0))
        self.assertEqual(cm.exception.code, "completed")
        cur = self.db.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM face_scans WHERE face_profile_id=%s", (self.wife,))
        self.assertEqual(int(cur.fetchone()["n"]), 1)
        cur.execute("SELECT COUNT(*) AS n FROM face_events WHERE request_uuid=%s AND "
                    "event_type='face.scan_request.completed'", (row["request_uuid"],))
        self.assertEqual(int(cur.fetchone()["n"]), 1)

    def test_completion_requires_consent(self):
        row, token = self._create()
        with self.assertRaises(self.fsi.InviteError) as cm:
            self.fsi.complete(self.db, row, MEAS)
        self.assertEqual(cm.exception.code, "consent_required")
        self.assertTrue(self.fsi.is_usable(self.fsi.by_uuid(self.db, row["request_uuid"])))

    def test_cancel_wins_over_an_open_page(self):
        row, token = self._create()
        row = self.fsi.record_consent(self.db, self.fsi.mark_opened(self.db, row))
        self.fsi.cancel(self.db, C1, row["request_uuid"])
        with self.assertRaises(self.fsi.InviteError) as cm:
            self.fsi.complete(self.db, row, MEAS)
        self.assertEqual(cm.exception.code, "inactive")
        self.assertIsNone(self.fp.get_profile(self.db, C1, self.wife)["latest_scan_id"])

    def test_cancel_is_owner_only(self):
        row, _ = self._create()
        with self.assertRaises(self.fp.ProfileError) as cm:
            self.fsi.cancel(self.db, C2, row["request_uuid"])
        self.assertEqual(cm.exception.status, 404)

    def test_resend_replaces_and_old_token_dies(self):
        r1, t1 = self._create()
        r2, t2 = self._create(channel="email", dest=EMAIL_OK)
        self.assertNotEqual(t1, t2)
        self.assertEqual(self.fsi.by_uuid(self.db, r1["request_uuid"])["status"], "CANCELLED")
        self.assertIsNone(self.fsi.by_token(self.db, t1)["active_slot"])
        self.assertFalse(self.fsi.is_usable(self.fsi.by_token(self.db, t1)))
        self.assertEqual(self.fsi.for_profile(self.db, C1, self.wife)["request_uuid"], r2["request_uuid"])

    def test_profile_deletion_cancels_the_request(self):
        row, token = self._create()
        self.assertEqual(self.fsi.cancel_for_profile(self.db, C1, self.wife), 1)
        self.fp.delete_profile(self.db, C1, self.wife)
        live = self.fsi.by_token(self.db, token)
        self.assertEqual(live["status"], "CANCELLED")
        with self.assertRaises(self.fp.ProfileError):
            self.fsi.complete(self.db, live, MEAS)

    def test_rate_limits(self):
        env = {"FACE_SCAN_REQUEST_MAX_PER_PROFILE_PER_DAY": "2",
               "FACE_SCAN_REQUEST_MAX_PER_CUSTOMER_PER_DAY": "10",
               "FACE_SCAN_REQUEST_MAX_PER_DESTINATION_PER_DAY": "10"}
        self._create(environ=env)
        self._create(environ=env)
        with self.assertRaises(self.fsi.InviteError) as cm:
            self._create(environ=env)
        self.assertEqual(cm.exception.status, 429)
        lim = self.fsi.IpLimiter(max_hits=2, window_seconds=60)
        self.assertTrue(lim.allow("ip", now=100))
        self.assertTrue(lim.allow("ip", now=101))
        self.assertFalse(lim.allow("ip", now=102))
        self.assertTrue(lim.allow("ip", now=161))

    def test_expire_due_marks_and_emits_once(self):
        row, _ = self._create()
        cur = self.db.cursor()
        cur.execute("UPDATE face_scan_invites SET expires_at=%s WHERE request_uuid=%s",
                    (datetime.now() - timedelta(minutes=1), row["request_uuid"]))
        self.db.commit()
        self.assertEqual(self.fsi.expire_due(self.db), 1)
        self.assertEqual(self.fsi.expire_due(self.db), 0)
        live = self.fsi.by_uuid(self.db, row["request_uuid"])
        self.assertEqual(live["status"], "EXPIRED")
        self.assertIsNone(live["active_slot"])
        cur.execute("SELECT COUNT(*) AS n FROM face_events WHERE request_uuid=%s AND "
                    "event_type='face.scan_request.expired'", (row["request_uuid"],))
        self.assertEqual(int(cur.fetchone()["n"]), 1)

    def test_delivery_failure_keeps_the_request_and_records_state(self):
        row, token = self._create()
        seen = {}

        def wa(phone, template, components):
            seen.update(phone=phone, template=template, components=components)
            return {"ok": False, "request_id": "", "error": "http_401"}

        row = self.fsi.send(self.db, row, token, whatsapp=wa, environ={})
        self.assertEqual(seen["phone"], "919999900001")
        self.assertEqual(seen["template"], "face_scan_request")
        self.assertEqual(seen["components"]["body_1"]["value"], "Sudhanshu")
        self.assertIn(token, seen["components"]["body_2"]["value"])
        self.assertTrue(seen["components"]["body_2"]["value"].startswith(
            "https://optiwar.com/f/"))
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["delivery_status"], "FAILED")
        self.assertEqual(row["delivery_error"], "http_401")
        self.assertTrue(self.fsi.is_usable(row))
        row = self.fsi.send(self.db, row, token,
                            whatsapp=lambda *a: {"ok": True, "request_id": "m1"}, environ={})
        self.assertEqual((row["delivery_status"], row["delivery_ref"]), ("SENT", "m1"))
        cur = self.db.cursor()
        cur.execute("SELECT event_type, payload FROM face_events WHERE request_uuid=%s ORDER BY id",
                    (row["request_uuid"],))
        evs = cur.fetchall()
        self.assertIn("face.scan_request.delivery_failed", [e["event_type"] for e in evs])
        self.assertIn("face.scan_request.sent", [e["event_type"] for e in evs])
        for e in evs:
            self.assertNotIn(token, e["payload"] or "")

    def test_email_delivery_carries_the_link_and_a_raise_is_recorded(self):
        row, token = self._create(channel="email", dest=EMAIL_OK)
        sent = {}

        def mail(to, subject, html, text, sender=None):
            sent.update(to=to, subject=subject, html=html, text=text, sender=sender)

        row = self.fsi.send(self.db, row, token, mailer=mail, environ={})
        self.assertEqual(sent["to"], EMAIL_OK)
        self.assertEqual(sent["sender"], "Optiwar Support <support@optiwar.com>")
        self.assertEqual(sent["subject"], "Optiwar \u2014 Face measurement request from Sudhanshu")
        self.assertIn(token, sent["html"])
        self.assertIn(token, sent["text"])
        for body in (sent["html"], sent["text"]):
            self.assertIn("Sudhanshu has invited you to complete a face measurement", body)
            self.assertIn("You do not need an Optiwar account or login", body)
            self.assertIn("Complete Face Scan", body)
            self.assertIn("valid for 24 hours and can be used only for this face scan request", body)
            self.assertIn("consent and permission to use your camera", body)
            self.assertIn("do not recognise the sender, simply ignore this email", body)
            self.assertIn("Safety notice: No payment is required", body)
            self.assertIn("Factory Outlet Opticals", body)
        self.assertEqual(row["delivery_status"], "SENT")

        row3, t3 = self._create(channel="email", dest=EMAIL_OK)
        self.fsi.send(self.db, row3, t3, mailer=mail,
                      environ={"FACE_SCAN_REQUEST_MAIL_SENDER": "admin@optiwar.com"})
        self.assertEqual(sent["sender"], "admin@optiwar.com")

        def boom(*a, **k):
            raise RuntimeError("smtp down")

        row2, t2 = self._create(channel="email", dest=EMAIL_OK)
        row2 = self.fsi.send(self.db, row2, t2, mailer=boom)
        self.assertEqual((row2["delivery_status"], row2["delivery_error"]), ("FAILED", "RuntimeError"))
        self.assertTrue(self.fsi.is_usable(row2))

    def test_public_view_hides_the_destination_and_the_token(self):
        row, token = self._create()
        v = self.fsi.public_view(row)
        self.assertNotIn("token_hash", v)
        self.assertNotEqual(v["destination"], WA_OK)
        self.assertTrue(v["destination"].endswith("0001"))
        self.assertTrue(v["active"])
        self.assertNotIn(token, str(v))

    def test_gate_needs_both_flags(self):
        base = {"FACE_PROFILES_ENABLED": "1", "FACE_PROFILES_ALLOW_EMAILS": "lensebazaar@gmail.com"}
        self.assertFalse(self.fsi.enabled_for("lensebazaar@gmail.com", dict(base)))
        self.assertTrue(self.fsi.enabled_for("lensebazaar@gmail.com",
                                             dict(base, FACE_REMOTE_SCAN_ENABLED="1")))
        self.assertFalse(self.fsi.enabled_for("x@example.com",
                                              dict(base, FACE_REMOTE_SCAN_ENABLED="1")))
        self.assertFalse(self.fsi.enabled_for("lensebazaar@gmail.com",
                                              {"FACE_REMOTE_SCAN_ENABLED": "1"}))

    def test_assistant_is_told_about_requests_only_for_an_admitted_account(self):
        env = {"FACE_PROFILES_ENABLED": "1", "FACE_REMOTE_SCAN_ENABLED": "1",
               "FACE_PROFILES_ALLOW_EMAILS": "lensebazaar@gmail.com"}
        section = self.fsi.assistant_prompt_section("lensebazaar@gmail.com", env)
        self.assertIn("Send a link", section)
        self.assertIn("WhatsApp or", section)
        self.assertIn("[ACTION:NAVIGATE:/profile/?tab=faces]", section)
        self.assertIn("answer is yes", section)
        self.assertEqual(self.fsi.assistant_prompt_section("x@example.com", env), "")
        self.assertEqual(self.fsi.assistant_prompt_section("", env), "")
        self.assertEqual(self.fsi.assistant_prompt_section(
            "lensebazaar@gmail.com", dict(env, FACE_REMOTE_SCAN_ENABLED="0")), "")


# What a phone's browser sends when it opens a page served with
# ``Referrer-Policy: no-referrer`` and posts a form from it: no Referer at all,
# and ``Origin: null``. The site-wide guard read this as "missing" and 403'd
# the consent POST in production. Same shape for a fetch() from that page.
MOBILE_POST = {"Origin": "null"}
PROD = "https://optiwar.in/"


def _prod_client_class():
    from flask.testing import FlaskClient

    class ProdClient(FlaskClient):
        """Every request and cookie lives on the canonical production host."""

        def open(self, *a, **kw):
            kw.setdefault("base_url", PROD)
            return super().open(*a, **kw)

        def session_transaction(self, *a, **kw):
            kw.setdefault("base_url", PROD)
            return super().session_transaction(*a, **kw)

    return ProdClient


@unittest.skipUnless(AVAILABLE, "MariaDB test database not reachable")
class RouteTests(unittest.TestCase):
    """Owner API + guest pages through a Flask test client, with the
    scanner template rendered for real, delivery stubbed, and the production
    Origin/Referer guard installed and *enforcing* against the canonical host
    — a bare test app never saw the 403 the phone did."""

    @classmethod
    def setUpClass(cls):
        from flask import Blueprint, Flask
        cls.fp = _load_service()
        cls.db = _connect()
        cls.db.cursor().execute(LEGACY_DDL)
        cls.db.commit()
        cls.fp.ensure_schema(cls.db)
        mods = _load_pkg(cls.fp, lambda: cls.db)
        cls.fsi = mods["face_scan_invites"]
        cls.fsi.ensure_schema(cls.db)
        cls.fpa = mods["face_profiles_api"]
        cls.api = mods["face_scan_invites_api"]
        cls.guard = mods["csrf_guard"]
        cls.root = os.path.join(tempfile.mkdtemp(), "flaskr")
        os.makedirs(cls.root)
        cls.app = Flask(__name__, root_path=cls.root,
                        template_folder=os.path.join(REPO, "templates"))
        cls.app.config.update(TESTING=True, SECRET_KEY="test",
                              FACE_PROFILES_ENABLED="1",
                              FACE_REMOTE_SCAN_ENABLED="1",
                              FACE_PROFILES_ALLOW_EMAILS="lensebazaar@gmail.com",
                              CSRF_ENFORCE=True,
                              TRUSTED_HOSTS=["optiwar.com", "www.optiwar.com",
                                             "optiwar.in", "www.optiwar.in", "localhost"])
        cls.guard.init_csrf_guard(cls.app)
        cls.app.test_client_class = _prod_client_class()
        bp = Blueprint("main", __name__)
        cls.fpa.register(bp)
        cls.api.register(bp)
        cls.app.register_blueprint(bp)
        cls.app.static_folder = os.path.join(REPO, "static")
        cls.sent = []
        cls.fsi._default_whatsapp = lambda phone, template, components: (
            cls.sent.append(("wa", phone, components)) or {"ok": True, "request_id": "m1"})
        cls.fsi._default_mailer = lambda to, subject, html, text, sender=None: \
            cls.sent.append(("mail", to, text))

    @classmethod
    def tearDownClass(cls):
        _wipe_invites(cls.db, C1, C2)
        _wipe(cls.db, C1, C2)
        cls.db.close()

    def setUp(self):
        _wipe_invites(self.db, C1, C2)
        _wipe(self.db, C1, C2)
        self.sent[:] = []
        # the owner is a real browser on the canonical host: same-origin POSTs
        self.owner = self._browser(user_id=C1, user_email="lensebazaar@gmail.com", user_name="Sudhanshu")
        # the guest is the phone: cookies on production, headers per MOBILE_POST
        self.guest = self.app.test_client()
        r = self.owner.post("/api/face-profiles", json={"display_name": "Wife", "relationship_type": "spouse", "consent": True})
        self.wife = r.get_json()["profile"]["id"]

    def _browser(self, **sess):
        """A normal browser on the site: sends its Origin, may be signed in."""
        c = self.app.test_client()
        c.environ_base["HTTP_ORIGIN"] = "https://optiwar.in"
        if sess:
            with c.session_transaction() as s:
                s.update(**sess)
        return c

    def _send(self, channel="whatsapp", dest=WA_OK, pid=None):
        r = self.owner.post("/api/face-profiles/%d/scan-request" % (pid or self.wife),
                            json={"channel": channel, "destination": dest})
        return r

    def _gget(self, path, client=None):
        return (client or self.guest).get(path, base_url=PROD)

    def _csrf(self, client=None):
        with (client or self.guest).session_transaction() as s:
            return (s.get("face_scan_guest") or {}).get("csrf")

    def _consent(self, client=None, csrf="auto", **form):
        c = client or self.guest
        data = {"consent": "1"}
        data.update(form)
        if csrf == "auto":
            csrf = self._csrf(c)
        if csrf is not None:
            data["_guest_csrf"] = csrf
        return c.post("/face-scan/guest/consent", data=data, base_url=PROD,
                      headers=MOBILE_POST)

    def _save(self, body=None, client=None, csrf="auto"):
        c = client or self.guest
        headers = dict(MOBILE_POST)
        if csrf == "auto":
            csrf = self._csrf(c)
        if csrf is not None:
            headers["X-Face-Scan-Csrf"] = csrf
        return c.post("/face-scan/guest/api/save", json=body or MEAS,
                      base_url=PROD, headers=headers)

    def _token(self):
        kind, _, payload = self.sent[-1]
        link = payload["body_2"]["value"] if kind == "wa" else \
            [w for w in payload.split() if "/f/" in w][0]
        return link.rsplit("/", 1)[1]

    def test_owner_creates_and_gets_the_link_once_to_share_by_hand(self):
        """The message carries the link, and so does the create response —
        the sender can copy it when WhatsApp/email never arrives. Later
        status reads cannot rebuild it."""
        r = self._send()
        self.assertEqual(r.status_code, 201)
        j = r.get_json()
        body = j["scan_request"]
        self.assertEqual((body["status"], body["delivery_status"], body["channel"]),
                         ("PENDING", "SENT", "whatsapp"))
        token = self._token()
        self.assertEqual(j["link"], self.sent[-1][2]["body_2"]["value"])
        self.assertTrue(j["link"].endswith("/f/" + token))
        self.assertNotIn("token", body)
        self.assertNotIn("link", body)
        r = self.owner.get("/api/face-profiles/%d/scan-request" % self.wife)
        self.assertEqual(r.get_json()["scan_request"]["request_uuid"], body["request_uuid"])
        self.assertNotIn(token, r.get_data(as_text=True))
        self.assertNotIn("link", r.get_json())
        # the link from the response opens the guest flow like the one in the message
        self.assertEqual(self._gget("/f/" + token).status_code, 303)

    def test_link_is_handed_out_even_when_delivery_failed(self):
        self.fsi._default_whatsapp = lambda phone, template, components: {"ok": False, "error": "http_401"}
        try:
            r = self._send()
        finally:
            self.fsi._default_whatsapp = lambda phone, template, components: (
                self.sent.append(("wa", phone, components)) or {"ok": True, "request_id": "m1"})
        j = r.get_json()
        self.assertEqual(j["scan_request"]["delivery_status"], "FAILED")
        self.assertIn("/f/", j["link"])
        self.assertEqual(self._gget("/f/" + j["link"].rsplit("/", 1)[1]).status_code, 303)

    def test_legacy_long_path_still_opens(self):
        self._send()
        self.assertEqual(self._gget("/face-scan/request/" + self._token()).status_code, 303)

    def test_anonymous_and_ungated_owner_calls(self):
        anon = self._browser()
        self.assertEqual(anon.post("/api/face-profiles/%d/scan-request" % self.wife, json={}).status_code, 401)
        with anon.session_transaction() as s:
            s.update(user_id=C2, user_email="x@example.com", user_name="X")
        self.assertEqual(anon.post("/api/face-profiles/%d/scan-request" % self.wife,
                                   json={"channel": "whatsapp", "destination": WA_OK}).status_code, 404)
        self.assertEqual(anon.post("/api/face-profiles/%d/scan-request/cancel" % self.wife).status_code, 404)

    def test_foreign_profile_is_404_even_inside_the_gate(self):
        other = self._browser(user_id=C2, user_email="lensebazaar@gmail.com", user_name="Twin")
        r = other.post("/api/face-profiles/%d/scan-request" % self.wife,
                       json={"channel": "whatsapp", "destination": WA_OK})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.sent, [])

    def test_guest_flow_end_to_end(self):
        """The journey the phone takes, against the enforcing guard on the
        canonical host: open, consent, scan, save — none of it 403s."""
        self._send()
        token = self._token()
        r = self._gget("/f/" + token)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
        self.assertTrue(r.headers["Location"].endswith("/face-scan/guest"))
        # refresh of the entry link does not spend it
        self.assertEqual(self._gget("/f/" + token).status_code, 303)
        r = self._gget("/face-scan/guest")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn("Sudhanshu", page)
        self.assertIn("Wife", page)
        self.assertNotIn("lensebazaar@gmail.com", page)
        self.assertNotIn(token, page)
        csrf = self._csrf()
        self.assertTrue(csrf and len(csrf) >= 32)
        self.assertIn('name="_guest_csrf" value="%s"' % csrf, page)
        # the form posts to the same origin it was served from
        self.assertIn('action="/face-scan/guest/consent"', page)
        # scanner before consent redirects back
        self.assertEqual(self._gget("/face-scan/guest/scan").status_code, 303)
        r = self._consent(consent="")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Please tick", r.get_data(as_text=True))
        r = self._consent()
        self.assertEqual(r.status_code, 303, r.get_data(as_text=True))
        r = self._gget("/face-scan/guest/scan")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn("window.OW_GUEST", page)
        self.assertIn("/face-scan/guest/api", page)
        self.assertIn('"csrf": "%s"' % csrf, page)
        self.assertNotIn("/api/tryon/save", page.split("<script")[0])
        self.assertNotIn(token, page)
        # the only control on the page says in words what it does
        self.assertIn('id="startBtnText"', page)
        self.assertIn("Hold the phone at arm's length", page)
        self.assertIn('id="backLink" hidden', page)
        self.assertNotIn("https://", page.split("window.OW_GUEST")[1].split("</script>")[0])
        self.assertEqual(self._gget("/face-scan/guest/api/my-measurements").get_json(),
                         {"has_measurements": False})
        self.assertEqual(self._gget("/face-scan/guest/api/matching-frames").status_code, 404)
        r = self._save(dict(MEAS, screenshot="data:image/jpeg;base64,/9j/4AAA"))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self._gget("/face-scan/guest/done").status_code, 200)
        # spent: the guest session no longer resolves and the link says completed
        self.assertEqual(self._save().status_code, 410)
        r = self._gget("/f/" + token)
        self.assertEqual(r.status_code, 410)
        self.assertIn("already complete", r.get_data(as_text=True))
        # the owner's card sees it
        r = self.owner.get("/api/face-profiles/%d" % self.wife)
        self.assertTrue(r.get_json()["profile"]["has_scan"])
        r = self.owner.get("/api/face-profiles/%d/scan-request" % self.wife)
        self.assertEqual(r.get_json()["scan_request"]["status"], "COMPLETED")
        # the guest never gained an owner session
        self.assertEqual(self._gget("/api/face-profiles").status_code, 401)

    def test_guest_posts_need_the_guest_secret_not_the_origin(self):
        """The guest POST is authorised by the binding + secret. Without the
        secret it is refused even with a perfect Origin; with it, Origin is
        irrelevant (``null``, absent, or foreign — the page cannot send one)."""
        self._send()
        self._gget("/f/" + self._token())
        good = self._csrf()
        for hdrs in ({"Origin": "https://optiwar.in"}, {}, {"Origin": "https://evil.example"}):
            r = self.guest.post("/face-scan/guest/consent", data={"consent": "1"},
                                base_url=PROD, headers=hdrs)
            self.assertEqual(r.status_code, 403, hdrs)
            self.assertIn("not valid", r.get_data(as_text=True))
        r = self.guest.post("/face-scan/guest/consent",
                            data={"consent": "1", "_guest_csrf": "x" * len(good)},
                            base_url=PROD, headers=MOBILE_POST)
        self.assertEqual(r.status_code, 403)
        # consent never recorded by any of those
        self.assertEqual(self._gget("/face-scan/guest/scan").status_code, 303)
        for hdrs in ({}, {"Origin": "null"}, {"Origin": "https://optiwar.in"}):
            self.guest.post("/face-scan/guest/consent", data={"consent": "1", "_guest_csrf": good},
                            base_url=PROD, headers=hdrs)
            self.assertEqual(self._gget("/face-scan/guest/scan").status_code, 200, hdrs)
        # save: header missing / wrong -> 403 and nothing written; right -> 200
        self.assertEqual(self._save(csrf=None).status_code, 403)
        self.assertEqual(self._save(csrf="y" * len(good)).status_code, 403)
        self.assertFalse(self.owner.get("/api/face-profiles/%d" % self.wife).get_json()["profile"]["has_scan"])
        r = self.guest.post("/face-scan/guest/api/save", json=MEAS, base_url=PROD,
                            headers={"X-Face-Scan-Csrf": good})  # no Origin at all
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_guest_secret_is_bound_to_its_own_session(self):
        """A secret lifted from one guest's page does nothing in another
        browser: the binding it belongs to is not there."""
        self._send()
        self._gget("/f/" + self._token())
        good = self._csrf()
        stranger = self.app.test_client()
        r = stranger.post("/face-scan/guest/consent", data={"consent": "1", "_guest_csrf": good},
                          base_url=PROD, headers=MOBILE_POST)
        self.assertEqual(r.status_code, 410)
        r = stranger.post("/face-scan/guest/api/save", json=MEAS, base_url=PROD,
                          headers=dict(MOBILE_POST, **{"X-Face-Scan-Csrf": good}))
        self.assertEqual(r.status_code, 410)

    def test_guest_binding_from_before_the_secret_is_reopened_not_trusted(self):
        """A guest session minted by the previous release has no secret; it
        is treated as no binding, and re-opening the link re-mints it."""
        self._send()
        token = self._token()
        self._gget("/f/" + token)
        with self.guest.session_transaction() as s:
            s["face_scan_guest"] = {k: v for k, v in s["face_scan_guest"].items() if k != "csrf"}
        self.assertEqual(self._gget("/face-scan/guest").status_code, 404)
        self.assertEqual(self._gget("/f/" + token).status_code, 303)
        self.assertEqual(self._consent().status_code, 303)

    def test_owner_api_keeps_the_normal_csrf_protection(self):
        """The exemption is two exact guest endpoints. The signed-in owner's
        own mutations still need a trusted Origin/Referer."""
        evil = self.app.test_client()
        with evil.session_transaction() as s:
            s.update(user_id=C1, user_email="lensebazaar@gmail.com", user_name="Sudhanshu")
        for hdrs in ({"Origin": "https://evil.example"}, {}, {"Origin": "null"}):
            r = evil.post("/api/face-profiles/%d/scan-request" % self.wife,
                          json={"channel": "whatsapp", "destination": WA_OK},
                          base_url=PROD, headers=hdrs)
            self.assertEqual(r.status_code, 403, hdrs)
            self.assertEqual(evil.post("/api/face-profiles/%d/scan-request/cancel" % self.wife,
                                       base_url=PROD, headers=hdrs).status_code, 403)
            self.assertEqual(evil.delete("/api/face-profiles/%d" % self.wife,
                                         base_url=PROD, headers=hdrs).status_code, 403)
        self.assertEqual(self.sent, [])
        r = evil.post("/api/face-profiles/%d/scan-request" % self.wife,
                      json={"channel": "whatsapp", "destination": WA_OK},
                      base_url=PROD, headers={"Referer": "https://www.optiwar.in/profile"})
        self.assertEqual(r.status_code, 201)
        exempt = self.guard.CSRF_EXEMPT_ENDPOINTS
        self.assertEqual({e for e in exempt if "face" in e},
                         {"main.face_scan_guest_consent_post", "main.face_scan_guest_save"})
        self.assertNotIn("*", "".join(self.app.config["TRUSTED_HOSTS"]))

    def test_scan_lands_in_the_invited_profile_whatever_the_browser_is_logged_into(self):
        """A: logged out. B: same customer's browser. C: another customer's
        browser. The scan reaches the invited profile and only that one."""
        other = self._browser(user_id=C2, user_email="lensebazaar@gmail.com", user_name="Twin")
        other_self = other.get("/api/face-profiles").get_json()["profiles"][0]["id"]
        me = self.owner.get("/api/face-profiles").get_json()["profiles"][0]["id"]
        for label, browser in (("A", self.app.test_client()), ("B", self.owner), ("C", other)):
            _wipe_invites(self.db, C1)
            self._send()
            token = self._token()
            self.assertEqual(self._gget("/f/" + token, browser).status_code, 303, label)
            self.assertEqual(self._consent(browser).status_code, 303, label)
            r = self._save(dict(MEAS, pd_far=60.0 + len(label)), browser)
            self.assertEqual(r.status_code, 200, (label, r.get_data(as_text=True)))
            wife = self.owner.get("/api/face-profiles/%d" % self.wife).get_json()["profile"]
            self.assertTrue(wife["has_scan"], label)
            self.assertFalse(self.owner.get("/api/face-profiles/%d" % me).get_json()["profile"]["has_scan"], label)
            self.assertFalse(other.get("/api/face-profiles/%d" % other_self).get_json()["profile"]["has_scan"], label)
            cur = self.db.cursor()
            cur.execute("DELETE FROM face_scans WHERE face_profile_id=%s", (self.wife,))
            cur.execute("UPDATE face_profiles SET latest_scan_id=NULL WHERE id=%s", (self.wife,))
            self.db.commit()
        # B and C still hold their own sessions, untouched
        self.assertEqual(self.owner.get("/api/face-profiles").status_code, 200)
        self.assertEqual(other.get("/api/face-profiles").get_json()["profiles"][0]["id"], other_self)
        self.assertEqual(self.app.test_client().get("/api/face-profiles").status_code, 401)

    def test_invalid_expired_cancelled_tokens_are_generic(self):
        r = self.guest.get("/face-scan/request/notatoken")
        self.assertEqual(r.status_code, 404)
        self.assertIn("not valid", r.get_data(as_text=True))
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self._send()
        token = self._token()
        self.owner.post("/api/face-profiles/%d/scan-request/cancel" % self.wife)
        r = self._gget("/f/" + token)
        self.assertEqual(r.status_code, 410)
        self.assertIn("no longer active", r.get_data(as_text=True))
        self._send()
        token = self._token()
        cur = self.db.cursor()
        cur.execute("UPDATE face_scan_invites SET expires_at=NOW() - INTERVAL 1 MINUTE WHERE active_slot=1")
        self.db.commit()
        self.assertEqual(self._gget("/f/" + token).status_code, 410)

    def test_cancel_after_open_beats_the_open_page(self):
        self._send()
        token = self._token()
        self._gget("/f/" + token)
        self._consent()
        self.assertEqual(self._gget("/face-scan/guest/scan").status_code, 200)
        self.owner.post("/api/face-profiles/%d/scan-request/cancel" % self.wife)
        r = self._save()
        self.assertEqual(r.status_code, 410)
        self.assertFalse(self.owner.get("/api/face-profiles/%d" % self.wife).get_json()["profile"]["has_scan"])

    def test_resend_via_route_and_old_link_dies(self):
        self._send()
        t1 = self._token()
        r = self.owner.post("/api/face-profiles/%d/scan-request/resend" % self.wife, json={})
        self.assertEqual(r.status_code, 201)
        t2 = self._token()
        self.assertNotEqual(t1, t2)
        self.assertEqual(self._gget("/f/" + t1).status_code, 410)
        self.assertEqual(self._gget("/f/" + t2).status_code, 303)

    def test_delete_profile_cancels_and_reports(self):
        self._send()
        token = self._token()
        r = self.owner.delete("/api/face-profiles/%d" % self.wife)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["cancelled_scan_requests"], 1)
        self.assertEqual(self._gget("/f/" + token).status_code, 410)

    def test_self_profile_refused_and_email_channel_works(self):
        me = self.owner.get("/api/face-profiles").get_json()["profiles"][0]["id"]
        self.assertEqual(self._send(pid=me).status_code, 409)
        r = self._send(channel="email", dest=EMAIL_OK)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.sent[-1][0], "mail")
        token = self._token()
        self.assertEqual(self._gget("/f/" + token).status_code, 303)

    def test_route_refuses_the_account_holders_own_phone_and_email(self):
        """Acceptance C/D: the owner's own contacts as a destination for
        another person create no request, no event, no message."""
        _seed_customer(self.db, C1, "Sudhanshu Bhasin", "lensebazaar@gmail.com", "9810113801")
        for channel, dest, code in (("whatsapp", "9810113801", "own_phone"),
                                    ("whatsapp", "+91 98101 13801", "own_phone"),
                                    ("email", "LenseBazaar@gmail.com", "own_email")):
            r = self._send(channel=channel, dest=dest)
            self.assertEqual(r.status_code, 422, r.get_data(as_text=True))
            body = r.get_json()
            self.assertEqual(body["error"], code)
            self.assertIn("your Optiwar account", body["message"])
            self.assertNotIn("link", body)
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.fsi.for_customer(self.db, C1).get(self.wife))
        cur = self.db.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM face_events WHERE customer_id=%s", (C1,))
        self.assertEqual(cur.fetchone()["n"], 0)
        # the session email counts too when the customers row has none
        cur.execute("UPDATE customers SET customer_email=NULL, customer_phone=NULL "
                    "WHERE customer_id=%s", (C1,))
        self.db.commit()
        self.assertEqual(self._send(channel="email", dest="lensebazaar@gmail.com").status_code, 422)
        self.assertEqual(self._send(channel="whatsapp", dest="9810113801").status_code, 201)

    # -- the completion notice: the owner hears once, with labelled numbers --

    @property
    def fsd(self):
        return sys.modules["fsi_pkg.face_scan_done"]

    def _seed_owner(self, name="Sudhanshu Bhasin", email="lensebazaar@gmail.com"):
        _seed_customer(self.db, C1, name, email, "9810113801")

    def _events(self, etype):
        cur = self.db.cursor()
        cur.execute("SELECT * FROM face_events WHERE customer_id=%s AND event_type=%s",
                    (C1, etype))
        return cur.fetchall()

    def _remote_complete(self, before_save=None):
        self._send()
        token = self._token()
        self._gget("/f/" + token)
        self._consent()
        self.sent[:] = []
        if before_save:
            before_save()
        r = self._save()
        self.assertEqual(r.status_code, 200)
        return r.get_json()["scan_id"]

    def test_remote_completion_notifies_the_owner_not_the_guest(self):
        self._seed_owner()
        sid = self._remote_complete()
        kinds = sorted(k for k, _, _ in self.sent)
        self.assertEqual(kinds, ["mail", "wa"])
        wa = [s for s in self.sent if s[0] == "wa"][0]
        self.assertEqual(wa[1], "919810113801")          # the account, not WA_OK
        vals = [wa[2]["body_%d" % i]["value"] for i in range(1, 7)]
        self.assertEqual(vals, ["Sudhanshu Bhasin", "Wife (Spouse)", "61", "58.5", "131", "49-23-140"])
        mail = [s for s in self.sent if s[0] == "mail"][0]
        self.assertEqual(mail[1], "lensebazaar@gmail.com")
        self.assertIn("PD (distance):          61 mm", mail[2])
        self.assertIn("Wife (Spouse)", mail[2])
        self.assertIn("https://optiwar.in/profile/?tab=faces", mail[2])   # the sending site
        self.assertNotIn("/f/", mail[2])
        self.assertEqual(len(self._events(self.fsd.EV_COMPLETED)), 1)
        self.assertEqual(len(self._events(self.fsd.EV_NOTIFIED)), 2)
        self.assertEqual(self._events(self.fsd.EV_COMPLETED)[0]["scan_id"], sid)

    def test_a_second_call_for_the_same_scan_sends_nothing(self):
        self._seed_owner()
        sid = self._remote_complete()
        self.sent[:] = []
        with self.app.test_request_context():
            out = self.fsd.notify(self.db, sid, "optiwar.com", environ={
                "FACE_PROFILES_ENABLED": "1", "FACE_REMOTE_SCAN_ENABLED": "1",
                "FACE_PROFILES_ALLOW_EMAILS": "lensebazaar@gmail.com"})
        self.assertEqual(out, {"sent": False, "reason": "duplicate"})
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self._events(self.fsd.EV_NOTIFIED)), 2)

    def test_owners_own_tryon_scan_is_labelled_you(self):
        self._seed_owner()
        # the try-on route lives in models.py; exercise the helper it calls
        with self.app.test_request_context(base_url=PROD):
            from flask import session
            session["user_id"] = C1
            session["user_email"] = "lensebazaar@gmail.com"
            session["user_name"] = "Sudhanshu"
            sid, row = self.fpa.save_scan_from_tryon(self.db, dict(MEAS), None)
        self.assertTrue(row["is_self"])
        wa = [s for s in self.sent if s[0] == "wa"][0]
        self.assertEqual(wa[2]["body_2"]["value"], "Sudhanshu (you)")
        self.assertEqual(len(self._events(self.fsd.EV_COMPLETED)), 1)

    def test_account_outside_the_gate_hears_nothing(self):
        self._seed_owner("Someone", "other@example.com")
        self._remote_complete()
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self._events(self.fsd.EV_COMPLETED)), 0)

    def test_kill_switch_stops_the_notice_but_not_the_scan(self):
        self._seed_owner()
        self.app.config["FACE_SCAN_DONE_NOTIFY_ENABLED"] = "0"
        try:
            sid = self._remote_complete()
        finally:
            self.app.config.pop("FACE_SCAN_DONE_NOTIFY_ENABLED")
        self.assertTrue(sid)
        self.assertEqual(self.sent, [])

    def test_a_failed_provider_is_a_record_not_a_failed_save(self):
        self._seed_owner()
        old = self.fsi._default_whatsapp

        def down(*a):
            raise RuntimeError("msg91 down")

        def break_provider():
            self.fsi._default_whatsapp = down
        try:
            sid = self._remote_complete(before_save=break_provider)
        finally:
            self.fsi._default_whatsapp = old
        self.assertTrue(sid)
        failed = self._events(self.fsd.EV_NOTIFY_FAILED)
        self.assertEqual(len(failed), 1)
        self.assertIn("msg91 down", failed[0]["payload"])
        self.assertEqual([k for k, _, _ in self.sent], ["mail"])

    def test_whatsapp_template_record_has_no_variable_at_either_end(self):
        body = self.fsd.WA_TEMPLATE_BODY
        self.assertFalse(body.startswith("{{"))
        self.assertFalse(body.rstrip().endswith("}}"))
        for i in range(1, 7):
            self.assertIn("{{%d}}" % i, body)
        self.assertNotIn("{{7}}", body)


if __name__ == "__main__":
    unittest.main()


class MyFacesGroupPanelTests(unittest.TestCase):
    """The My Faces tab offers 'Scan several people' only when there is more
    than one person and remote scans are on, and shows each open group's
    distinct-member tally with no link or token in the page."""

    def _render(self, **ctx):
        from flask import Flask
        app = Flask(__name__, template_folder=os.path.join(REPO, "templates"))
        with open(os.path.join(REPO, "templates", "profile.html")) as fh:
            src = fh.read()
        start = src.index('<div id="tab-myface"')
        end = src.index("{% elif face_data %}", start)
        block = src[start:end] + "{% endif %}"
        if ctx.pop("with_script", False):
            block += src[src.index("<script>", end):src.rindex("</script>") + len("</script>")]
        base = dict(face_profiles_enabled=True, face_remote_scan_enabled=True,
                    face_relationships=[{"code": "parent", "label": "Parent"}],
                    face_scan_groups_open=[], face_profiles=[])
        base.update(ctx)
        with app.app_context():
            return app.jinja_env.from_string(block).render(**base)

    def _people(self, n):
        out = [{"id": 1, "display_name": "Sudhanshu", "is_self": True, "is_default": True,
                "has_scan": False, "relationship_label": "Me", "relationship_type": "self",
                "scan_request": None, "measurements": None}]
        for i in range(2, n + 1):
            out.append({"id": i, "display_name": "Person %d" % i, "is_self": False,
                        "is_default": False, "has_scan": False, "relationship_label": "Parent",
                        "relationship_type": "parent", "scan_request": None,
                        "measurements": None})
        return out

    def test_button_needs_two_people_and_remote_scans(self):
        btn = 'onclick="mfgOpen()"'
        self.assertNotIn(btn, self._render(face_profiles=self._people(1)))
        self.assertNotIn(btn, self._render(face_profiles=self._people(3),
                                           face_remote_scan_enabled=False))
        html_ = self._render(face_profiles=self._people(3))
        self.assertIn(btn, html_)
        self.assertEqual(html_.count('class="mfg-pick"'), 3)

    def test_open_group_shows_distinct_tally_and_no_link(self):
        people = self._people(3)
        people[1]["scan_request"] = {"active": True, "status": "OPENED", "opened": True,
                                     "consented": False, "channel": "whatsapp",
                                     "destination": "+91 98…801", "delivery_status": "SENT",
                                     "expires_in_seconds": 3600, "request_uuid": "u"}
        group = {"group_uuid": "g-1", "status": "OPEN", "required": 2, "completed": 1,
                 "members": [
                     {"face_profile_id": 2, "person": "Person 2 (Parent)", "status": "PENDING",
                      "scan_request": people[1]["scan_request"]},
                     {"face_profile_id": 3, "person": "Person 3 (Parent)", "status": "COMPLETED",
                      "scan_request": None}]}
        html_ = self._render(face_profiles=people, face_scan_groups_open=[group])
        self.assertIn("1 of 2 done", html_)
        self.assertIn("Person 2 (Parent) &middot; link opened", html_)
        self.assertIn("Person 3 (Parent) &middot; done", html_)
        self.assertIn('data-mf="group-cancel" data-guuid="g-1"', html_)
        self.assertNotIn("/f/", html_)
        # stage-1 rows are status-aware: pending and measured start unticked
        rows = re.findall(r'<label class="mf-person"[^>]*>\s*<input[^>]*>', html_)
        self.assertEqual(len(rows), 3)
        self.assertIn('data-pending="1"', rows[1])
        self.assertNotIn(" checked", rows[1])
        self.assertIn("Request pending", html_)

    def _person_row(self, html_, pid):
        m = re.search(r'<label class="mf-person" data-pid="%d"[^>]*>\s*<input[^>]*>' % pid, html_)
        self.assertIsNotNone(m, pid)
        return m.group(0)

    def test_stage1_defaults_measured_people_off_and_self_local_only(self):
        people = self._people(3)
        people[0]["has_scan"] = True
        people[0]["measurements"] = {"measured_at": "2026-09-12T10:00:00", "pd_far": 60.5,
                                     "pd_near": 58.0, "face_width": 130.5,
                                     "recommended_size": "48-24-140"}
        html_ = self._render(face_profiles=people)
        me, p2 = self._person_row(html_, 1), self._person_row(html_, 2)
        self.assertIn('data-self="1"', me)
        self.assertIn('data-measured="1"', me)
        self.assertNotIn(" checked", me)
        self.assertIn(" checked", p2)
        self.assertIn("Already measured 2026-09-12", html_)
        self.assertIn("No measurements yet", html_)
        self.assertIn("Measure several people", html_)
        self.assertNotIn("Scan several people", html_)
        # no destination input is rendered server-side at all: the field is
        # created per person only after "Send link" is chosen (never hidden+required)
        self.assertNotIn('class="mfg-dest"', html_)
        self.assertNotIn('class="mfg-ch"', html_)
        self.assertEqual(re.findall(r'<input[^>]*required[^>]*hidden', html_), [])
        for sid in ("mfgStage1", "mfgStage2", "mfgStage3", "mfgLinks"):
            self.assertIn('id="%s"' % sid, html_)
        self.assertIn('role="dialog"', html_)

    def test_rendered_script_is_valid_javascript_and_self_never_gets_a_link(self):
        """The My Faces script, as the browser receives it, parses; the plan
        renderer gives Self a fixed 'Scan here' and no Send-link control."""
        import shutil
        import subprocess
        html_ = self._render(face_profiles=self._people(3), with_script=True,
                             addresses=[], focus_face=2, is_india=True)
        scripts = re.findall(r"<script>(.*?)</script>", html_, re.S)
        self.assertTrue(scripts)
        js = "\n".join(scripts)
        self.assertIn("how:p.self?'here'", js)
        self.assertIn("a link is never sent to you", js)
        self.assertIn("mf-body-lock", js)
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
        try:
            r = subprocess.run([node, "--check", fh.name], capture_output=True, text=True)
        finally:
            os.unlink(fh.name)
        self.assertEqual(r.returncode, 0, r.stderr)


class MyFacesButtonsTests(unittest.TestCase):
    """A person's name is never interpolated into JavaScript: the card
    buttons carry their arguments as data attributes and one delegated
    listener reads them. (Flask's ``tojson`` in a double-quoted ``onclick``
    ended the attribute and killed every button that took a name.)"""

    def test_card_buttons_carry_data_attributes_not_inline_handlers(self):
        with open(os.path.join(REPO, "templates", "profile.html")) as fh:
            src = fh.read()
        self.assertEqual(re.findall(r'onclick=.{0,40}\|tojson', src), [])
        for fn in ("mfOpenScan(", "mfOpenEdit(", "mfDelete(", "mfSetDefault(", "mfCancelRequest("):
            self.assertEqual(re.findall(r'onclick=[\'"]' + re.escape(fn), src), [], fn)
        for kind in ("scan", "cancel", "default", "edit", "delete", "group-cancel"):
            self.assertIn('data-mf="%s"' % kind, src)
        self.assertIn("closest('[data-mf]')", src)
        self.assertEqual(re.findall(r'mfgCancel\([^)]*\{\{', src), [])

    def test_awkward_names_render_into_attributes_that_parse_back(self):
        from flask import Flask
        from markupsafe import escape
        import html
        app = Flask(__name__, template_folder=os.path.join(REPO, "templates"))
        names = ['Mother "Home"', "O'Connor", "<b>x</b>", "Am\u00e9lie \u4e2d"]
        with app.app_context():
            src = app.jinja_env.from_string(
                '{% for n in names %}<button data-mf="edit" data-pid="1" data-name="{{ n }}" '
                "data-request='{{ {\"destination\": n}|tojson|forceescape }}'></button>{% endfor %}"
            ).render(names=names)
        for n in names:
            attr = str(escape(n))
            self.assertIn('data-name="%s"' % attr, src)
            self.assertEqual(html.unescape(attr), n)
        # no raw quote or angle bracket survives inside an attribute value
        self.assertNotIn('data-name="Mother "', src)
        self.assertNotIn("<b>", src)
        import json
        for m in re.finditer(r"data-request='([^']*)'", src):
            self.assertIn(json.loads(html.unescape(m.group(1)))["destination"], names)
