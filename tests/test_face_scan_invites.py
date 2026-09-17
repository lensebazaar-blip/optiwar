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
    for name in ("face_scan_invites", "face_profiles_api", "face_scan_invites_api"):
        spec = importlib.util.spec_from_file_location(
            pkg_name + "." + name, os.path.join(REPO, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        out[name] = mod
    return out


def _wipe_invites(db, *cids):
    cur = db.cursor()
    for cid in cids:
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


@unittest.skipUnless(AVAILABLE, "MariaDB test database not reachable")
class RouteTests(unittest.TestCase):
    """Owner API + guest pages through a Flask test client, with the
    scanner template rendered for real and delivery stubbed."""

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
        cls.root = os.path.join(tempfile.mkdtemp(), "flaskr")
        os.makedirs(cls.root)
        cls.app = Flask(__name__, root_path=cls.root,
                        template_folder=os.path.join(REPO, "templates"))
        cls.app.config.update(TESTING=True, SECRET_KEY="test",
                              FACE_PROFILES_ENABLED="1",
                              FACE_REMOTE_SCAN_ENABLED="1",
                              FACE_PROFILES_ALLOW_EMAILS="lensebazaar@gmail.com")
        bp = Blueprint("main", __name__)
        cls.fpa.register(bp)
        cls.api.register(bp)
        cls.app.register_blueprint(bp)
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
        self.owner = self.app.test_client()
        self.guest = self.app.test_client()
        with self.owner.session_transaction() as s:
            s.update(user_id=C1, user_email="lensebazaar@gmail.com", user_name="Sudhanshu")
        r = self.owner.post("/api/face-profiles", json={"display_name": "Wife", "relationship_type": "spouse", "consent": True})
        self.wife = r.get_json()["profile"]["id"]

    def _send(self, channel="whatsapp", dest=WA_OK, pid=None):
        r = self.owner.post("/api/face-profiles/%d/scan-request" % (pid or self.wife),
                            json={"channel": channel, "destination": dest})
        return r

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
        self.assertEqual(self.guest.get("/f/" + token).status_code, 303)

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
        self.assertEqual(self.guest.get("/f/" + j["link"].rsplit("/", 1)[1]).status_code, 303)

    def test_legacy_long_path_still_opens(self):
        self._send()
        self.assertEqual(self.guest.get("/face-scan/request/" + self._token()).status_code, 303)

    def test_anonymous_and_ungated_owner_calls(self):
        anon = self.app.test_client()
        self.assertEqual(anon.post("/api/face-profiles/%d/scan-request" % self.wife, json={}).status_code, 401)
        with anon.session_transaction() as s:
            s.update(user_id=C2, user_email="x@example.com", user_name="X")
        self.assertEqual(anon.post("/api/face-profiles/%d/scan-request" % self.wife,
                                   json={"channel": "whatsapp", "destination": WA_OK}).status_code, 404)
        self.assertEqual(anon.post("/api/face-profiles/%d/scan-request/cancel" % self.wife).status_code, 404)

    def test_foreign_profile_is_404_even_inside_the_gate(self):
        other = self.app.test_client()
        with other.session_transaction() as s:
            s.update(user_id=C2, user_email="lensebazaar@gmail.com", user_name="Twin")
        r = other.post("/api/face-profiles/%d/scan-request" % self.wife,
                       json={"channel": "whatsapp", "destination": WA_OK})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.sent, [])

    def test_guest_flow_end_to_end(self):
        self._send()
        token = self._token()
        r = self.guest.get("/f/" + token)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
        self.assertTrue(r.headers["Location"].endswith("/face-scan/guest"))
        # refresh of the entry link does not spend it
        self.assertEqual(self.guest.get("/f/" + token).status_code, 303)
        r = self.guest.get("/face-scan/guest")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn("Sudhanshu", page)
        self.assertIn("Wife", page)
        self.assertNotIn("lensebazaar@gmail.com", page)
        self.assertNotIn(token, page)
        # scanner before consent redirects back
        self.assertEqual(self.guest.get("/face-scan/guest/scan").status_code, 303)
        self.assertEqual(self.guest.post("/face-scan/guest/consent", data={}).status_code, 200)
        r = self.guest.post("/face-scan/guest/consent", data={"consent": "1"})
        self.assertEqual(r.status_code, 303)
        r = self.guest.get("/face-scan/guest/scan")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn("window.OW_GUEST", page)
        self.assertIn("/face-scan/guest/api", page)
        self.assertNotIn("/api/tryon/save", page.split("<script")[0])
        self.assertNotIn(token, page)
        self.assertEqual(self.guest.get("/face-scan/guest/api/my-measurements").get_json(),
                         {"has_measurements": False})
        self.assertEqual(self.guest.get("/face-scan/guest/api/matching-frames").status_code, 404)
        r = self.guest.post("/face-scan/guest/api/save",
                            json=dict(MEAS, screenshot="data:image/jpeg;base64,/9j/4AAA"))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.guest.get("/face-scan/guest/done").status_code, 200)
        # spent: the guest session no longer resolves and the link says completed
        self.assertEqual(self.guest.post("/face-scan/guest/api/save", json=MEAS).status_code, 410)
        r = self.guest.get("/f/" + token)
        self.assertEqual(r.status_code, 410)
        self.assertIn("already complete", r.get_data(as_text=True))
        # the owner's card sees it
        r = self.owner.get("/api/face-profiles/%d" % self.wife)
        self.assertTrue(r.get_json()["profile"]["has_scan"])
        r = self.owner.get("/api/face-profiles/%d/scan-request" % self.wife)
        self.assertEqual(r.get_json()["scan_request"]["status"], "COMPLETED")
        # the guest never gained an owner session
        self.assertEqual(self.guest.get("/api/face-profiles").status_code, 401)

    def test_invalid_expired_cancelled_tokens_are_generic(self):
        r = self.guest.get("/face-scan/request/notatoken")
        self.assertEqual(r.status_code, 404)
        self.assertIn("not valid", r.get_data(as_text=True))
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self._send()
        token = self._token()
        self.owner.post("/api/face-profiles/%d/scan-request/cancel" % self.wife)
        r = self.guest.get("/f/" + token)
        self.assertEqual(r.status_code, 410)
        self.assertIn("no longer active", r.get_data(as_text=True))
        self._send()
        token = self._token()
        cur = self.db.cursor()
        cur.execute("UPDATE face_scan_invites SET expires_at=NOW() - INTERVAL 1 MINUTE WHERE active_slot=1")
        self.db.commit()
        self.assertEqual(self.guest.get("/f/" + token).status_code, 410)

    def test_cancel_after_open_beats_the_open_page(self):
        self._send()
        token = self._token()
        self.guest.get("/f/" + token)
        self.guest.post("/face-scan/guest/consent", data={"consent": "1"})
        self.assertEqual(self.guest.get("/face-scan/guest/scan").status_code, 200)
        self.owner.post("/api/face-profiles/%d/scan-request/cancel" % self.wife)
        r = self.guest.post("/face-scan/guest/api/save", json=MEAS)
        self.assertEqual(r.status_code, 410)
        self.assertFalse(self.owner.get("/api/face-profiles/%d" % self.wife).get_json()["profile"]["has_scan"])

    def test_resend_via_route_and_old_link_dies(self):
        self._send()
        t1 = self._token()
        r = self.owner.post("/api/face-profiles/%d/scan-request/resend" % self.wife, json={})
        self.assertEqual(r.status_code, 201)
        t2 = self._token()
        self.assertNotEqual(t1, t2)
        self.assertEqual(self.guest.get("/f/" + t1).status_code, 410)
        self.assertEqual(self.guest.get("/f/" + t2).status_code, 303)

    def test_delete_profile_cancels_and_reports(self):
        self._send()
        token = self._token()
        r = self.owner.delete("/api/face-profiles/%d" % self.wife)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["cancelled_scan_requests"], 1)
        self.assertEqual(self.guest.get("/f/" + token).status_code, 410)

    def test_self_profile_refused_and_email_channel_works(self):
        me = self.owner.get("/api/face-profiles").get_json()["profiles"][0]["id"]
        self.assertEqual(self._send(pid=me).status_code, 409)
        r = self._send(channel="email", dest=EMAIL_OK)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.sent[-1][0], "mail")
        token = self._token()
        self.assertEqual(self.guest.get("/f/" + token).status_code, 303)


if __name__ == "__main__":
    unittest.main()
