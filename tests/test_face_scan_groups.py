"""Scan groups: distinct-member completion, exactly one completion event and
one owner email, mutable membership, and no group work for an ordinary scan.

Runs on the production-shaped fixture of test_face_scan_invites (real
templates, enforcing origin guard, stubbed delivery)."""
import importlib.util
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests"))

from test_face_profiles import AVAILABLE, C1, C2  # noqa: E402
from test_face_scan_invites import (MEAS, RouteTests, WA_OK,  # noqa: E402
                                    _seed_customer)


def _load_groups():
    """face_profiles_api imports face_scan_groups from the same throwaway
    package; the api module here is loaded fresh, the service module is the
    one face_profiles_api already bound (see setUpClass)."""
    pkg = "fsi_pkg"
    out = {"face_scan_groups": sys.modules[pkg + ".face_scan_groups"]}
    for name in ("face_scan_groups_api",):
        spec = importlib.util.spec_from_file_location(
            pkg + "." + name, os.path.join(REPO, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        out[name] = mod
    return out


@unittest.skipUnless(AVAILABLE, "MariaDB test database not reachable")
class GroupTests(RouteTests):

    @classmethod
    def setUpClass(cls):
        for name in ("fsi_pkg.face_scan_groups", "fsi_pkg.face_scan_groups_api"):
            sys.modules.pop(name, None)
        super().setUpClass()
        mods = _load_groups()
        cls.fsg = mods["face_scan_groups"]
        cls.fsg.ensure_schema(cls.db)
        # a second blueprint: the base class already registered its routes
        from flask import Blueprint
        bp = Blueprint("groups", __name__)
        mods["face_scan_groups_api"].register(bp)
        cls.app.register_blueprint(bp)

    def setUp(self):
        super().setUp()
        cur = self.db.cursor()
        cur.execute("DELETE m FROM face_scan_group_members m JOIN face_scan_groups g "
                    "ON g.id=m.group_id WHERE g.customer_id IN (%s,%s)", (C1, C2))
        cur.execute("DELETE FROM face_scan_groups WHERE customer_id IN (%s,%s)", (C1, C2))
        self.db.commit()
        _seed_customer(self.db, C1, "Sudhanshu Bhasin", "lensebazaar@gmail.com", "9810113801")
        r = self.owner.post("/api/face-profiles", json={
            "display_name": "Mother", "relationship_type": "parent", "consent": True})
        self.mother = r.get_json()["profile"]["id"]
        self.me = [p for p in self.owner.get("/api/face-profiles").get_json()["profiles"]
                   if p["is_self"]][0]["id"]

    # --- helpers -----------------------------------------------------------

    def _group(self, *pids):
        r = self.owner.post("/api/face-scan-groups", json={"profile_ids": list(pids)})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()["group"]

    def _get(self, guuid):
        return self.owner.get("/api/face-scan-groups/" + guuid).get_json()["group"]

    def _scan_here(self, guuid, pid, meas=None):
        r = self.owner.post("/api/face-scan-groups/%s/members/%d/scan" % (guuid, pid),
                            json=dict(meas or MEAS))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def _scan_by_link(self, guuid, pid, dest=WA_OK):
        r = self.owner.post("/api/face-scan-groups/%s/members/%d/scan" % (guuid, pid),
                            json={"channel": "whatsapp", "destination": dest})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        token = self._token()
        guest = self.app.test_client()
        self._gget("/f/" + token, client=guest)
        self._consent(client=guest)
        r = self._save(client=guest)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()["scan_id"]

    def _scan_post(self, guuid, pid):
        return self.owner.post("/api/face-scan-groups/%s/members/%d/scan" % (guuid, pid),
                               json=dict(MEAS))

    def _link_post(self, guuid, pid, dest=WA_OK):
        return self.owner.post("/api/face-scan-groups/%s/members/%d/scan" % (guuid, pid),
                               json={"channel": "whatsapp", "destination": dest})

    def _events(self, etype, guuid=None):
        cur = self.db.cursor()
        sql = "SELECT * FROM face_events WHERE customer_id=%s AND event_type=%s"
        args = [C1, etype]
        if guuid:
            sql += " AND scan_group_id=%s"
            args.append(guuid)
        cur.execute(sql, args)
        return cur.fetchall()

    def _group_mails(self):
        return [s for s in self.sent if s[0] == "mail" and "Every face scan" in s[2]]

    # --- tests -------------------------------------------------------------

    def test_a_group_completes_on_distinct_members_not_on_scan_count(self):
        g = self._group(self.wife, self.mother)
        self.assertEqual((g["status"], g["required"], g["completed"]), ("OPEN", 2, 0))
        # Wife scanned three times: still one member done, group still open
        for _ in range(3):
            out = self._scan_here(g["group_uuid"], self.wife)
        self.assertEqual((out["group"]["status"], out["group"]["completed"]), ("OPEN", 1))
        self.assertEqual(len(self._events(self.fsg.EV_COMPLETED, g["group_uuid"])), 0)
        self.assertEqual(self._group_mails(), [])
        # Mother by remote link: second distinct member -> complete
        self.sent[:] = []
        sid = self._scan_by_link(g["group_uuid"], self.mother)
        g2 = self._get(g["group_uuid"])
        self.assertEqual((g2["status"], g2["completed"], g2["required"]), ("COMPLETED", 2, 2))
        mother = [m for m in g2["members"] if m["face_profile_id"] == self.mother][0]
        self.assertEqual((mother["status"], mother["scan_id"], mother["person"]),
                         ("COMPLETED", sid, "Mother (Parent)"))
        self.assertEqual(len(self._events(self.fsg.EV_COMPLETED, g["group_uuid"])), 1)
        mails = self._group_mails()
        self.assertEqual(len(mails), 1)
        self.assertEqual(mails[0][1], "lensebazaar@gmail.com")
        self.assertIn("Wife (Spouse)", mails[0][2])
        self.assertIn("Mother (Parent)", mails[0][2])
        self.assertIn("PD (distance):          61 mm", mails[0][2])
        self.assertNotIn("/f/", mails[0][2])
        # the per-scan notices still went out for each landing (owner, not guest)
        self.assertTrue(any(s[0] == "wa" and s[1] == "919810113801" for s in self.sent))

    def test_completion_is_emitted_once_under_retry(self):
        g = self._group(self.wife)
        out = self._scan_here(g["group_uuid"], self.wife)
        self.assertEqual(out["group"]["status"], "COMPLETED")
        sid = out["scan_id"]
        # a worker/refresh re-delivers the same landing; and a later re-scan
        with self.app.test_request_context():
            self.fsg.on_scan_completed(self.db, C1, self.wife, sid, g["group_uuid"])
            self.fsg.on_scan_completed(self.db, C1, self.wife, sid + 1, g["group_uuid"])
        r = self._scan_post(g["group_uuid"], self.wife)
        self.assertEqual(r.status_code, 409)   # closed: a scan into a finished group is refused
        self.assertEqual(len(self._events(self.fsg.EV_COMPLETED, g["group_uuid"])), 1)
        self.assertEqual(len(self._group_mails()), 1)
        self.assertEqual(len(self._events(self.fsg.EV_NOTIFIED, g["group_uuid"])), 1)

    def test_membership_is_mutable_and_removal_can_complete(self):
        g = self._group(self.wife)
        r = self.owner.post("/api/face-scan-groups/%s/members" % g["group_uuid"],
                            json={"face_profile_id": self.mother})
        self.assertEqual(r.get_json()["group"]["required"], 2)
        # adding the same person twice is a no-op
        r = self.owner.post("/api/face-scan-groups/%s/members" % g["group_uuid"],
                            json={"face_profile_id": self.mother})
        self.assertEqual(r.get_json()["group"]["required"], 2)
        self._scan_here(g["group_uuid"], self.wife)
        # Mother has a pending link; removing her cancels it and completes the group
        self._link_post(g["group_uuid"], self.mother)
        self.assertEqual(self._get(g["group_uuid"])["status"], "OPEN")
        r = self.owner.delete("/api/face-scan-groups/%s/members/%d"
                              % (g["group_uuid"], self.mother))
        self.assertEqual(r.status_code, 200)
        g2 = r.get_json()["group"]
        self.assertEqual((g2["status"], g2["required"], g2["completed"]), ("COMPLETED", 1, 1))
        inv = self.owner.get("/api/face-profiles/%d/scan-request" % self.mother).get_json()
        self.assertEqual(inv["scan_request"]["status"], "CANCELLED")
        self.assertEqual(len(self._events(self.fsg.EV_COMPLETED, g["group_uuid"])), 1)

    def test_cancel_kills_pending_links_and_a_late_scan_does_not_complete(self):
        g = self._group(self.wife, self.mother)
        self._link_post(g["group_uuid"], self.mother)
        token = self._token()
        r = self.owner.post("/api/face-scan-groups/%s/cancel" % g["group_uuid"])
        self.assertEqual(r.get_json()["group"]["status"], "CANCELLED")
        self.assertIn(self._gget("/f/" + token).status_code, (404, 410))  # the link is dead
        r = self._scan_post(g["group_uuid"], self.wife)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self._events(self.fsg.EV_COMPLETED, g["group_uuid"])), 0)
        self.assertEqual(self._group_mails(), [])

    def test_an_ordinary_scan_never_touches_a_group(self):
        g = self._group(self.wife)
        # the plain card link (no group) and the owner's own try-on save
        self._send()
        token = self._token()
        self._gget("/f/" + token)
        self._consent()
        self.assertEqual(self._save().status_code, 200)
        with self.app.test_request_context(base_url="https://optiwar.in/"):
            from flask import session
            session.update(user_id=C1, user_email="lensebazaar@gmail.com", user_name="Sudhanshu")
            self.fpa.save_scan_from_tryon(self.db, dict(MEAS), None)
        g2 = self._get(g["group_uuid"])
        self.assertEqual((g2["status"], g2["completed"]), ("OPEN", 0))
        self.assertEqual(len(self._events(self.fsg.EV_MEMBER_COMPLETED, g["group_uuid"])), 0)
        self.assertEqual(self._group_mails(), [])

    def test_deleting_a_member_profile_leaves_the_group(self):
        g = self._group(self.wife, self.mother)
        self._scan_here(g["group_uuid"], self.wife)
        r = self.owner.delete("/api/face-profiles/%d" % self.mother)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["left_scan_groups"], 1)
        g2 = self._get(g["group_uuid"])
        self.assertEqual((g2["status"], g2["required"]), ("COMPLETED", 1))

    def test_somebody_elses_group_does_not_exist(self):
        g = self._group(self.wife)
        other = self._browser(user_id=C2, user_email="lensebazaar@gmail.com", user_name="Other")
        self.assertEqual(other.get("/api/face-scan-groups/" + g["group_uuid"]).status_code, 404)
        self.assertEqual(other.post("/api/face-scan-groups/%s/members" % g["group_uuid"],
                                    json={"face_profile_id": self.wife}).status_code, 404)
        # and a stranger's profile cannot be put in my group
        r = self.owner.post("/api/face-scan-groups", json={"profile_ids": [999999]})
        self.assertEqual(r.status_code, 404)
        r = self.owner.post("/api/face-scan-groups", json={"profile_ids": []})
        self.assertEqual(r.status_code, 400)

    def test_group_email_failure_is_a_record_not_a_failed_scan(self):
        g = self._group(self.wife)
        old = self.fsi._default_mailer

        def down(*a, **k):
            raise RuntimeError("smtp down")
        self.fsi._default_mailer = down
        try:
            out = self._scan_here(g["group_uuid"], self.wife)
        finally:
            self.fsi._default_mailer = old
        self.assertEqual(out["group"]["status"], "COMPLETED")
        failed = self._events(self.fsg.EV_NOTIFY_FAILED, g["group_uuid"])
        self.assertEqual(len(failed), 1)
        self.assertIn("smtp down", failed[0]["payload"])

    def test_an_expired_group_link_leaves_the_member_pending(self):
        g = self._group(self.wife, self.mother)
        self._scan_here(g["group_uuid"], self.wife)
        r = self._link_post(g["group_uuid"], self.mother)
        self.assertEqual(r.status_code, 201)
        token = self._token()
        cur = self.db.cursor()
        cur.execute("UPDATE face_scan_invites SET expires_at=NOW() - INTERVAL 1 HOUR "
                    "WHERE request_uuid=%s", (r.get_json()["scan_request"]["request_uuid"],))
        self.db.commit()
        with self.app.test_request_context():
            self.fsi.expire_due(self.db)
        self.assertIn(self._gget("/f/" + token).status_code, (404, 410))
        g2 = self._get(g["group_uuid"])
        self.assertEqual((g2["status"], g2["completed"], g2["required"]), ("OPEN", 1, 2))
        mother = [m for m in g2["members"] if m["face_profile_id"] == self.mother][0]
        self.assertEqual(mother["status"], "PENDING")
        self.assertEqual(len(self._events(self.fsg.EV_COMPLETED, g["group_uuid"])), 0)
        # the owner can send Mother a fresh link and the group still completes
        self._scan_by_link(g["group_uuid"], self.mother)
        self.assertEqual(self._get(g["group_uuid"])["status"], "COMPLETED")

    def test_group_events_and_notice_carry_no_token_link_or_capture(self):
        g = self._group(self.wife, self.mother)
        self._scan_here(g["group_uuid"], self.wife)
        self.assertEqual(self._link_post(g["group_uuid"], self.mother).status_code, 201)
        token = self._token()
        self.sent[:] = []   # the invitation itself is the one message that carries the link
        guest = self.app.test_client()
        self._gget("/f/" + token, client=guest)
        self._consent(client=guest)
        self.assertEqual(self._save(client=guest).status_code, 200)
        self.assertEqual(self._get(g["group_uuid"])["status"], "COMPLETED")
        self.assertEqual(len(self._group_mails()), 1)
        cur = self.db.cursor()
        cur.execute("SELECT payload FROM face_events WHERE customer_id=%s "
                    "AND event_type LIKE 'face.scan_group.%%'", (C1,))
        payloads = [r["payload"] or "" for r in cur.fetchall()]
        self.assertTrue(payloads)
        blob = "\n".join(payloads) + "\n".join(
            s[2] if isinstance(s[2], str) else json.dumps(s[2]) for s in self.sent)
        for forbidden in (token, "/f/", "captures", "landmarks", "screenshot", "http"):
            if forbidden == "http":
                # the notice may link to the owner's own profile page, nothing else
                links = [w for w in blob.split() if w.startswith("http")]
                self.assertTrue(all(w.endswith("/profile#my-faces") for w in links), links)
                continue
            self.assertNotIn(forbidden, blob)


# The base fixture's own tests belong to its module; only the group tests run here.
for _name in dir(RouteTests):
    if _name.startswith("test_"):
        setattr(GroupTests, _name, None)
del RouteTests


if __name__ == "__main__":
    unittest.main()
