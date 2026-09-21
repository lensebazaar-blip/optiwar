"""Face profiles: one customer, several faces, and the rules that hold them.

What matters here is not whether a row can be inserted. It is whether a
customer can end up with two Selves or none, whether removing the person who
was default leaves nobody default, whether the migration changes a single
number a customer already had measured, whether one customer's profile id
means anything in another customer's session (it must 404), and whether a raw
face photograph can still be fetched from a public path (it must not).

    OPTIWAR_TEST_MYSQL_DB=optiwar2 python3 -m unittest tests.test_face_profiles
"""
import importlib.util
import os
import stat
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_CONF = dict(
    host=os.environ.get("OPTIWAR_TEST_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("OPTIWAR_TEST_MYSQL_PORT", "3306")),
    user=os.environ.get("OPTIWAR_TEST_MYSQL_USER", "oslb6"),
    password=os.environ.get("OPTIWAR_TEST_MYSQL_PASSWORD", "testpw"),
    database=os.environ.get("OPTIWAR_TEST_MYSQL_DB", "optiwar2"),
)

# Production's face_measurements, minus the FK to customers (the test
# database has no customers table and the FK is not what is under test).
LEGACY_DDL = """
CREATE TABLE IF NOT EXISTS face_measurements (
  id int(11) NOT NULL AUTO_INCREMENT,
  customer_id int(11) NOT NULL,
  pd_far decimal(5,2) DEFAULT NULL,
  pd_near decimal(5,2) DEFAULT NULL,
  face_width decimal(5,2) DEFAULT NULL,
  eye_mouth decimal(5,2) DEFAULT NULL,
  recommended_diameter int(11) DEFAULT NULL,
  recommended_bridge int(11) DEFAULT NULL,
  recommended_length int(11) DEFAULT NULL,
  decentration decimal(5,2) DEFAULT NULL,
  frame_candidates text DEFAULT NULL,
  screenshot_path varchar(500) DEFAULT NULL,
  measured_at datetime DEFAULT current_timestamp(),
  PRIMARY KEY (id),
  KEY customer_id (customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# The owner's account as measured in production on 2026-09-13; the migration
# must reproduce these four values exactly.
BASELINE = dict(pd_far=Decimal("60.50"), pd_near=Decimal("58.00"),
                face_width=Decimal("130.50"), eye_mouth=Decimal("70.00"),
                recommended_diameter=48, recommended_bridge=24,
                recommended_length=140, decentration=Decimal("1.25"),
                frame_candidates='[{"size":"48-24-140","suitability":"best"}]')

JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 2048

# Customer ids no real fixture uses; each test class cleans its own.
C1, C2 = 9800498, 9800499


def _connect():
    import pymysql
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor,
                           autocommit=False, connect_timeout=5, **DB_CONF)


def _available():
    try:
        _connect().close()
        return True
    except Exception:  # noqa: BLE001
        return False


AVAILABLE = _available()


def _load_service():
    spec = importlib.util.spec_from_file_location(
        "face_profiles_under_test", os.path.join(REPO, "face_profiles.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_api(fp_mod, get_db):
    """face_profiles_api.py with its two package imports satisfied by stubs."""
    pkg_name = "fp_pkg"
    # Every fp_pkg module must bind to *this* fp_mod: a sibling left over
    # from an earlier suite would raise a ProfileError the API cannot catch.
    for name in [n for n in sys.modules if n.startswith(pkg_name + ".")]:
        del sys.modules[name]
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [REPO]
    sys.modules[pkg_name] = pkg
    sys.modules[pkg_name + ".face_profiles"] = fp_mod
    db_mod = types.ModuleType(pkg_name + ".db")
    db_mod.get_db = get_db
    sys.modules[pkg_name + ".db"] = db_mod
    spec = importlib.util.spec_from_file_location(
        pkg_name + ".face_scan_invites",
        os.path.join(REPO, "face_scan_invites.py"))
    fsi = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fsi
    spec.loader.exec_module(fsi)
    spec = importlib.util.spec_from_file_location(
        pkg_name + ".face_profiles_api",
        os.path.join(REPO, "face_profiles_api.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _wipe(db, *customer_ids):
    cur = db.cursor()
    for cid in customer_ids:
        cur.execute("DELETE FROM face_scans WHERE customer_id=%s", (cid,))
        cur.execute("DELETE FROM face_profiles WHERE customer_id=%s", (cid,))
        cur.execute("DELETE FROM face_measurements WHERE customer_id=%s", (cid,))
    db.commit()


def _legacy_row(db, cid, measured_at=None, **overrides):
    vals = dict(BASELINE)
    vals.update(overrides)
    cur = db.cursor()
    cur.execute(
        "INSERT INTO face_measurements (customer_id, pd_far, pd_near, face_width, "
        "eye_mouth, recommended_diameter, recommended_bridge, recommended_length, "
        "decentration, frame_candidates, screenshot_path, measured_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (cid, vals["pd_far"], vals["pd_near"], vals["face_width"],
         vals["eye_mouth"], vals["recommended_diameter"],
         vals["recommended_bridge"], vals["recommended_length"],
         vals["decentration"], vals["frame_candidates"],
         "tryon/captures/legacy_%d.jpg" % cid,
         measured_at or datetime(2026, 9, 13, 10, 0, 0)))
    db.commit()
    return cur.lastrowid


class GateTests(unittest.TestCase):
    """The flag and the allow-list, without a database."""

    def setUp(self):
        self.fp = _load_service()

    def test_off_by_default(self):
        self.assertFalse(self.fp.enabled_for("lensebazaar@gmail.com", None, None))
        self.assertFalse(self.fp.enabled_for("lensebazaar@gmail.com", "0", "lensebazaar@gmail.com"))

    def test_flag_alone_opens_nobody_when_an_allow_list_is_set(self):
        self.assertFalse(self.fp.enabled_for("someone@example.com", "1", "lensebazaar@gmail.com"))
        self.assertTrue(self.fp.enabled_for("LenseBazaar@gmail.com ", "1", "lensebazaar@gmail.com"))

    def test_flag_without_allow_list_opens_every_signed_in_customer(self):
        self.assertTrue(self.fp.enabled_for("someone@example.com", "true", ""))

    def test_relationship_and_name_cleaning(self):
        self.assertEqual(self.fp.clean_relationship("Spouse"), "spouse")
        with self.assertRaises(self.fp.ProfileError):
            self.fp.clean_relationship("self")
        with self.assertRaises(self.fp.ProfileError):
            self.fp.clean_relationship("cousin")
        with self.assertRaises(self.fp.ProfileError):
            self.fp.clean_name("   ")
        self.assertEqual(self.fp.clean_name("  Priya  "), "Priya")


@unittest.skipUnless(AVAILABLE, "no MariaDB test database (see scripts/setup_test_db.sh)")
class InvariantTests(unittest.TestCase):
    """Exactly one Self, exactly one default, and the operations that must
    not be able to break either."""

    @classmethod
    def setUpClass(cls):
        cls.fp = _load_service()
        cls.db = _connect()
        cls.db.cursor().execute(LEGACY_DDL)
        cls.db.commit()
        cls.fp.ensure_schema(cls.db)

    @classmethod
    def tearDownClass(cls):
        _wipe(cls.db, C1, C2)
        cls.db.close()

    def setUp(self):
        _wipe(self.db, C1, C2)

    def _counts(self, cid):
        cur = self.db.cursor()
        cur.execute("SELECT SUM(is_self) s, SUM(is_default) d, COUNT(*) n "
                    "FROM face_profiles WHERE customer_id=%s AND is_active=1", (cid,))
        r = cur.fetchone()
        return int(r["s"] or 0), int(r["d"] or 0), int(r["n"])

    def test_ensure_self_is_idempotent_and_named_after_the_account(self):
        a = self.fp.ensure_self(self.db, C1, "Sudhanshu")
        b = self.fp.ensure_self(self.db, C1, "Somebody Else")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(a["display_name"], "Sudhanshu")
        self.assertTrue(a["is_self"] and a["is_default"])
        self.assertEqual(self._counts(C1), (1, 1, 1))

    def test_a_second_self_cannot_be_inserted(self):
        self.fp.ensure_self(self.db, C1, "A")
        cur = self.db.cursor()
        with self.assertRaises(Exception):
            cur.execute("INSERT INTO face_profiles (customer_id, display_name, "
                        "relationship_type, is_self, self_slot) VALUES (%s,'B','self',1,1)",
                        (C1,))
        self.db.rollback()
        self.assertEqual(self._counts(C1), (1, 1, 1))

    def test_create_requires_consent_for_another_person(self):
        self.fp.ensure_self(self.db, C1, "A")
        with self.assertRaises(self.fp.ProfileError) as e:
            self.fp.create_profile(self.db, C1, "Wife", "spouse", consent=False)
        self.assertEqual(e.exception.status, 400)
        p = self.fp.create_profile(self.db, C1, "Wife", "spouse", consent=True)
        self.assertIsNotNone(p["consent_recorded_at"])
        self.assertFalse(p["is_default"])
        self.assertEqual(self._counts(C1), (1, 1, 2))

    def test_create_cannot_make_another_self(self):
        self.fp.ensure_self(self.db, C1, "A")
        with self.assertRaises(self.fp.ProfileError):
            self.fp.create_profile(self.db, C1, "Me again", "self", consent=True)

    def test_set_default_moves_it_and_never_leaves_two(self):
        me = self.fp.ensure_self(self.db, C1, "A")
        wife = self.fp.create_profile(self.db, C1, "Wife", "spouse", consent=True)
        self.fp.set_default(self.db, C1, wife["id"])
        self.assertEqual(self.fp.default_profile(self.db, C1)["id"], wife["id"])
        self.assertEqual(self._counts(C1), (1, 1, 2))
        self.fp.set_default(self.db, C1, me["id"])
        self.assertEqual(self.fp.default_profile(self.db, C1)["id"], me["id"])
        self.assertEqual(self._counts(C1), (1, 1, 2))

    def test_deleting_the_default_restores_self_as_default(self):
        me = self.fp.ensure_self(self.db, C1, "A")
        wife = self.fp.create_profile(self.db, C1, "Wife", "spouse", consent=True)
        self.fp.set_default(self.db, C1, wife["id"])
        self.fp.delete_profile(self.db, C1, wife["id"])
        self.assertEqual(self.fp.default_profile(self.db, C1)["id"], me["id"])
        self.assertEqual(self._counts(C1), (1, 1, 1))
        with self.assertRaises(self.fp.NotFound):
            self.fp.require_profile(self.db, C1, wife["id"])

    def test_self_cannot_be_deleted_or_turned_into_someone_else(self):
        me = self.fp.ensure_self(self.db, C1, "A")
        with self.assertRaises(self.fp.ProfileError):
            self.fp.delete_profile(self.db, C1, me["id"])
        with self.assertRaises(self.fp.ProfileError):
            self.fp.rename_profile(self.db, C1, me["id"], relationship_type="child")
        renamed = self.fp.rename_profile(self.db, C1, me["id"], display_name="Sudhanshu K")
        self.assertEqual(renamed["display_name"], "Sudhanshu K")
        self.assertTrue(renamed["is_self"])
        self.assertEqual(renamed["relationship_type"], "self")

    def test_a_profile_of_another_customer_does_not_exist(self):
        self.fp.ensure_self(self.db, C1, "A")
        other = self.fp.create_profile(self.db, C2, "Kid", "child", consent=True)
        with self.assertRaises(self.fp.NotFound) as e:
            self.fp.require_profile(self.db, C1, other["id"])
        self.assertEqual(e.exception.status, 404)
        for op in (lambda: self.fp.set_default(self.db, C1, other["id"]),
                   lambda: self.fp.delete_profile(self.db, C1, other["id"]),
                   lambda: self.fp.rename_profile(self.db, C1, other["id"], display_name="x"),
                   lambda: self.fp.record_scan(self.db, C1, other["id"], BASELINE)):
            with self.assertRaises(self.fp.NotFound):
                op()
        # And it is still there for its owner, unchanged.
        self.assertEqual(self.fp.require_profile(self.db, C2, other["id"])["display_name"], "Kid")


@unittest.skipUnless(AVAILABLE, "no MariaDB test database (see scripts/setup_test_db.sh)")
class MigrationTests(unittest.TestCase):
    """Nobody rescans. The owner's four numbers come through exactly."""

    @classmethod
    def setUpClass(cls):
        cls.fp = _load_service()
        cls.db = _connect()
        cls.db.cursor().execute(LEGACY_DDL)
        cls.db.commit()
        cls.fp.ensure_schema(cls.db)

    @classmethod
    def tearDownClass(cls):
        _wipe(cls.db, C1, C2)
        cls.db.close()

    def setUp(self):
        _wipe(self.db, C1, C2)

    def test_legacy_measurement_becomes_the_self_scan_unchanged(self):
        _legacy_row(self.db, C1)
        self.fp.migrate_customer(self.db, C1, "Sudhanshu")
        me = self.fp.self_profile(self.db, C1)
        self.assertTrue(me["is_default"])
        self.assertEqual(me["pd_far"], Decimal("60.50"))
        self.assertEqual(me["pd_near"], Decimal("58.00"))
        self.assertEqual(me["face_width"], Decimal("130.50"))
        view = self.fp.public_view(me)
        self.assertEqual(view["measurements"]["recommended_size"], "48-24-140")
        self.assertEqual(self.fp.parity(self.db, C1), [])
        cur = self.db.cursor()
        cur.execute("SELECT status, legacy_measurement_id FROM face_scans WHERE customer_id=%s", (C1,))
        rows = cur.fetchall()
        self.assertEqual([r["status"] for r in rows], [self.fp.ST_COMPLETED])
        self.assertIsNotNone(rows[0]["legacy_measurement_id"])

    def test_migration_runs_twice_without_a_second_scan_or_self(self):
        _legacy_row(self.db, C1)
        self.fp.migrate_customer(self.db, C1, "A")
        self.fp.migrate_customer(self.db, C1, "A")
        self.fp.migrate_all(self.db, dry_run=False)
        cur = self.db.cursor()
        cur.execute("SELECT COUNT(*) n FROM face_scans WHERE customer_id=%s", (C1,))
        self.assertEqual(cur.fetchone()["n"], 1)
        cur.execute("SELECT COUNT(*) n FROM face_profiles WHERE customer_id=%s", (C1,))
        self.assertEqual(cur.fetchone()["n"], 1)

    def test_legacy_row_is_untouched_by_migration(self):
        _legacy_row(self.db, C1)
        cur = self.db.cursor()
        cur.execute("SELECT * FROM face_measurements WHERE customer_id=%s", (C1,))
        before = cur.fetchall()
        self.fp.migrate_customer(self.db, C1, "A")
        cur.execute("SELECT * FROM face_measurements WHERE customer_id=%s", (C1,))
        self.assertEqual(cur.fetchall(), before)

    def test_customer_without_a_measurement_gets_an_empty_self(self):
        self.fp.migrate_customer(self.db, C2, "New Person")
        me = self.fp.self_profile(self.db, C2)
        self.assertIsNone(me["pd_far"])
        self.assertEqual(me["display_name"], "New Person")

    def test_migrate_all_lists_then_does(self):
        _legacy_row(self.db, C1)
        _legacy_row(self.db, C2)
        dry = self.fp.migrate_all(self.db, dry_run=True)
        self.assertEqual(set(dry["pending"]) & {C1, C2}, {C1, C2})
        self.assertEqual(dry["migrated"], 0)
        self.assertIsNone(self.fp.self_profile(self.db, C1))
        self.fp.migrate_all(self.db, dry_run=False)
        self.assertEqual(self.fp.parity(self.db, C1), [])
        self.assertEqual(self.fp.parity(self.db, C2), [])

    def test_a_new_scan_on_the_default_person_mirrors_into_the_legacy_row(self):
        _legacy_row(self.db, C1)
        self.fp.migrate_customer(self.db, C1, "A")
        me = self.fp.self_profile(self.db, C1)
        self.fp.record_scan(self.db, C1, me["id"], dict(BASELINE, pd_far="61.00"))
        cur = self.db.cursor()
        cur.execute("SELECT pd_far FROM face_measurements WHERE customer_id=%s", (C1,))
        self.assertEqual([r["pd_far"] for r in cur.fetchall()], [Decimal("61.00")])
        cur.execute("SELECT status FROM face_scans WHERE customer_id=%s ORDER BY id", (C1,))
        self.assertEqual([r["status"] for r in cur.fetchall()],
                         [self.fp.ST_SUPERSEDED, self.fp.ST_COMPLETED])

    def test_a_scan_on_a_non_default_person_leaves_the_legacy_row_alone(self):
        _legacy_row(self.db, C1)
        self.fp.migrate_customer(self.db, C1, "A")
        wife = self.fp.create_profile(self.db, C1, "Wife", "spouse", consent=True)
        self.fp.record_scan(self.db, C1, wife["id"], dict(BASELINE, pd_far="64.00"))
        cur = self.db.cursor()
        cur.execute("SELECT pd_far FROM face_measurements WHERE customer_id=%s", (C1,))
        self.assertEqual([r["pd_far"] for r in cur.fetchall()], [Decimal("60.50")])
        self.assertEqual(self.fp.get_profile(self.db, C1, wife["id"])["pd_far"], Decimal("64.00"))


class CaptureStorageTests(unittest.TestCase):
    """The photograph is not under static/, is owner-only, and goes away."""

    def setUp(self):
        self.fp = _load_service()
        self.root = os.path.join(tempfile.mkdtemp(), "flaskr")
        os.makedirs(os.path.join(self.root, "static", "tryon", "captures"))

    def test_capture_lands_outside_the_web_root_with_owner_only_bits(self):
        rel = self.fp.store_capture(self.root, C1, JPEG)
        path = os.path.join(self.fp.capture_dir(self.root), os.path.basename(rel))
        self.assertTrue(os.path.exists(path))
        self.assertNotIn(os.sep + "static" + os.sep, path)
        self.assertIn(os.path.join("secure_uploads", "tryon", "captures"), path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertNotIn(str(C1), os.path.basename(rel).split("_")[-1])

    def test_legacy_captures_are_moved_out_of_static(self):
        legacy = os.path.join(self.root, "static", "tryon", "captures", "old.jpg")
        with open(legacy, "wb") as fh:
            fh.write(JPEG)
        moved = self.fp.relocate_legacy_captures(self.root)
        self.assertEqual(moved, ["old.jpg"])
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.exists(os.path.join(self.fp.capture_dir(self.root), "old.jpg")))
        self.assertEqual(self.fp.relocate_legacy_captures(self.root), [])


@unittest.skipUnless(AVAILABLE, "no MariaDB test database (see scripts/setup_test_db.sh)")
class RetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fp = _load_service()
        cls.db = _connect()
        cls.db.cursor().execute(LEGACY_DDL)
        cls.db.commit()
        cls.fp.ensure_schema(cls.db)

    @classmethod
    def tearDownClass(cls):
        _wipe(cls.db, C1)
        cls.db.close()

    def setUp(self):
        _wipe(self.db, C1)
        self.root = os.path.join(tempfile.mkdtemp(), "flaskr")
        os.makedirs(self.root)

    def test_capture_older_than_retention_is_removed_and_the_numbers_stay(self):
        me = self.fp.ensure_self(self.db, C1, "A")
        rel = self.fp.store_capture(self.root, C1, JPEG)
        sid = self.fp.record_scan(self.db, C1, me["id"], BASELINE, capture_path=rel)
        cur = self.db.cursor()
        cur.execute("UPDATE face_scans SET created_at=%s, measured_at=%s WHERE id=%s",
                    (datetime.utcnow() - timedelta(days=9),) * 2 + (sid,))
        self.db.commit()
        purged = self.fp.purge_due_captures(self.db, self.root, 7)
        self.assertEqual(purged, [sid])
        self.assertFalse(os.path.exists(os.path.join(self.fp.capture_dir(self.root),
                                                     os.path.basename(rel))))
        cur.execute("SELECT pd_far, capture_purged_at FROM face_scans WHERE id=%s", (sid,))
        row = cur.fetchone()
        self.assertEqual(row["pd_far"], Decimal("60.50"))
        self.assertIsNotNone(row["capture_purged_at"])
        self.assertEqual(self.fp.purge_due_captures(self.db, self.root, 7), [])

    def test_recent_capture_is_kept(self):
        me = self.fp.ensure_self(self.db, C1, "A")
        rel = self.fp.store_capture(self.root, C1, JPEG)
        self.fp.record_scan(self.db, C1, me["id"], BASELINE, capture_path=rel)
        self.assertEqual(self.fp.purge_due_captures(self.db, self.root, 7), [])
        self.assertTrue(os.path.exists(os.path.join(self.fp.capture_dir(self.root),
                                                    os.path.basename(rel))))

    def test_pending_assignment_scan_expires_after_retention(self):
        cur = self.db.cursor()
        cur.execute("INSERT INTO face_scans (customer_id, status, created_at) VALUES (%s,%s,%s)",
                    (C1, self.fp.ST_PENDING_ASSIGNMENT, datetime.utcnow() - timedelta(days=8)))
        cur.execute("INSERT INTO face_scans (customer_id, status, created_at) VALUES (%s,%s,%s)",
                    (C1, self.fp.ST_PENDING_ASSIGNMENT, datetime.utcnow()))
        self.db.commit()
        self.assertEqual(self.fp.expire_pending_scans(self.db, 7), 1)
        cur.execute("SELECT status FROM face_scans WHERE customer_id=%s ORDER BY id", (C1,))
        self.assertEqual([r["status"] for r in cur.fetchall()],
                         [self.fp.ST_EXPIRED, self.fp.ST_PENDING_ASSIGNMENT])

    def test_run_retention_reads_the_owner_set_windows(self):
        r = self.fp.run_retention(self.db, self.root,
                                  {"FACE_PENDING_SCAN_RETENTION_DAYS": "7",
                                   "FACE_RAW_CAPTURE_RETENTION_DAYS": "7"})
        self.assertEqual((r["pending_days"], r["capture_days"]), (7, 7))


@unittest.skipUnless(AVAILABLE, "no MariaDB test database (see scripts/setup_test_db.sh)")
class ApiTests(unittest.TestCase):
    """The routes: gate, ownership, 404 for a stranger's id, no photo path."""

    @classmethod
    def setUpClass(cls):
        from flask import Blueprint, Flask
        cls.fp = _load_service()
        cls.db = _connect()
        cls.db.cursor().execute(LEGACY_DDL)
        cls.db.commit()
        cls.fp.ensure_schema(cls.db)
        cls.api = _load_api(cls.fp, lambda: cls.db)
        cls.root = os.path.join(tempfile.mkdtemp(), "flaskr")
        os.makedirs(cls.root)
        cls.app = Flask(__name__, root_path=cls.root)
        cls.app.config.update(TESTING=True, SECRET_KEY="test",
                              FACE_PROFILES_ENABLED="1",
                              FACE_PROFILES_ALLOW_EMAILS="lensebazaar@gmail.com")
        bp = Blueprint("main", __name__)
        cls.api.register(bp)
        cls.app.register_blueprint(bp)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        _wipe(cls.db, C1, C2)
        cls.db.close()

    def setUp(self):
        _wipe(self.db, C1, C2)
        self._login(C1, "lensebazaar@gmail.com", "Sudhanshu")

    def _login(self, cid, email, name):
        with self.client.session_transaction() as s:
            s["user_id"] = cid
            s["user_email"] = email
            s["user_name"] = name

    def test_signed_out_is_401_and_a_customer_outside_the_list_is_404(self):
        with self.client.session_transaction() as s:
            s.clear()
        self.assertEqual(self.client.get("/api/face-profiles").status_code, 401)
        self._login(C2, "someone@example.com", "Other")
        self.assertEqual(self.client.get("/api/face-profiles").status_code, 404)

    def test_list_creates_self_from_the_account_name(self):
        r = self.client.get("/api/face-profiles")
        self.assertEqual(r.status_code, 200)
        rows = r.get_json()["profiles"]
        self.assertEqual([(p["display_name"], p["is_self"], p["is_default"]) for p in rows],
                         [("Sudhanshu", True, True)])
        self.assertNotIn("capture_path", rows[0])

    def test_create_rename_default_delete_cycle(self):
        r = self.client.post("/api/face-profiles", json={"display_name": "Wife", "relationship_type": "spouse"})
        self.assertEqual(r.status_code, 400)  # no consent
        r = self.client.post("/api/face-profiles", json={"display_name": "Wife", "relationship_type": "spouse", "consent": True})
        self.assertEqual(r.status_code, 201)
        wid = r.get_json()["profile"]["id"]
        r = self.client.patch("/api/face-profiles/%d" % wid, json={"display_name": "Priya"})
        self.assertEqual(r.get_json()["profile"]["display_name"], "Priya")
        r = self.client.post("/api/face-profiles/%d/default" % wid)
        self.assertTrue(r.get_json()["profile"]["is_default"])
        r = self.client.delete("/api/face-profiles/%d" % wid)
        self.assertEqual(r.status_code, 200)
        rows = self.client.get("/api/face-profiles").get_json()["profiles"]
        self.assertEqual([(p["is_self"], p["is_default"]) for p in rows], [(True, True)])

    def test_self_is_protected_over_http(self):
        me = self.client.get("/api/face-profiles").get_json()["profiles"][0]["id"]
        self.assertEqual(self.client.delete("/api/face-profiles/%d" % me).status_code, 409)
        r = self.client.patch("/api/face-profiles/%d" % me, json={"relationship_type": "child"})
        self.assertEqual(r.status_code, 409)

    def test_another_customers_profile_is_404_on_every_route(self):
        other = self.fp.create_profile(self.db, C2, "Kid", "child", consent=True)
        pid = other["id"]
        for method, url in (("get", "/api/face-profiles/%d"),
                            ("patch", "/api/face-profiles/%d"),
                            ("post", "/api/face-profiles/%d/default"),
                            ("delete", "/api/face-profiles/%d"),
                            ("get", "/api/face-profiles/%d/capture"),
                            ("get", "/api/face-profiles/%d/references")):
            r = getattr(self.client, method)(url % pid, json={"display_name": "x"})
            self.assertEqual(r.status_code, 404, (method, url))
        self.assertEqual(self.fp.require_profile(self.db, C2, pid)["display_name"], "Kid")

    def test_tryon_save_writes_the_chosen_profile_and_stores_the_photo_privately(self):
        wife = self.fp.create_profile(self.db, C1, "Wife", "spouse", consent=True)
        with self.app.test_request_context():
            from flask import session
            session["user_id"] = C1
            session["user_name"] = "Sudhanshu"
            sid, row = self.api.save_scan_from_tryon(
                self.db, dict(BASELINE, face_profile_id=wife["id"]), JPEG)
        self.assertEqual(row["id"], wife["id"])
        self.assertEqual(row["pd_far"], Decimal("60.50"))
        cur = self.db.cursor()
        cur.execute("SELECT capture_path FROM face_scans WHERE id=%s", (sid,))
        rel = cur.fetchone()["capture_path"]
        self.assertFalse(rel.startswith("static/"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "static", rel)))
        r = self.client.get("/api/face-profiles/%d/capture" % wife["id"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Cache-Control"], "private, no-store")
        self._login(C2, "lensebazaar@gmail.com", "Other")
        self.assertEqual(self.client.get("/api/face-profiles/%d/capture" % wife["id"]).status_code, 404)


if __name__ == "__main__":
    unittest.main()
