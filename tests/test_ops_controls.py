"""Ops controls (ops_controls.py): bounded settings with a change log, the
blinking indicators, and the routes Ops use to change them. No database."""
import datetime
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


class _Cursor:
    store, log, quota, flags, reviewed, sqls = {}, [], [], [], [], []
    _row = None

    def execute(self, sql, args=None):
        self._row, self._rows = None, []
        self.sqls.append(sql)
        if sql.startswith("SELECT setting_value") and args[0] in self.store:
            self._row = {"setting_value": self.store[args[0]]}
        elif sql.startswith("INSERT INTO ops_settings_log"):
            self.log.append(args)
        elif sql.startswith("INSERT INTO ops_settings"):
            self.store[args[0]] = args[1]
        elif sql.startswith("SELECT client_ip, read_count"):
            self._rows = list(self.quota)
        elif sql.startswith("SELECT COUNT(*) AS n FROM upload_flags"):
            self._row = {"n": len(self.flags)}
        elif sql.startswith("SELECT id, created_at"):
            self._rows = list(self.flags)
        elif sql.startswith("UPDATE upload_flags"):
            self.reviewed.append(args)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Bad:
    def execute(self, *a):
        raise RuntimeError("no table")


def _reset():
    _Cursor.store.clear()
    for lst in (_Cursor.sqls, _Cursor.log, _Cursor.quota, _Cursor.flags, _Cursor.reviewed):
        del lst[:]


def _plain():
    spec = importlib.util.spec_from_file_location("ops_controls_plain", os.path.join(REPO, "ops_controls.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Rules(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.oc = _plain()

    def setUp(self):
        _reset()

    def test_defaults_when_unset_or_unreadable(self):
        self.assertEqual(self.oc.get(_Cursor(), self.oc.NCR24_FEE), 1000)
        self.assertEqual(self.oc.get(_Bad(), self.oc.RX_PER_IP), 20)
        self.assertEqual(self.oc.get(_Bad(), self.oc.RX_PER_DAY), 300)

    def test_values_are_bounded_whole_numbers(self):
        self.assertEqual(self.oc.validate(self.oc.NCR24_FEE, "1250"), (1250, ""))
        for bad in (99, 10001, "abc", "12.5", True, None):
            self.assertIsNone(self.oc.validate(self.oc.NCR24_FEE, bad)[0], bad)
        self.assertIsNone(self.oc.validate("nope", 5)[0])

    def test_a_change_is_logged_with_who(self):
        self.assertEqual(self.oc.set_value(_Cursor(), self.oc.NCR24_FEE, 1250, "ops@x", now="t"), 1000)
        self.assertEqual(self.oc.get(_Cursor(), self.oc.NCR24_FEE), 1250)
        self.assertEqual(_Cursor.log[0], (self.oc.NCR24_FEE, 1000, 1250, "ops@x", "t"))

    def test_indicators_blink_near_and_at_the_limits(self):
        quiet = self.oc.indicators(_Cursor(), 20, 300)
        self.assertFalse(quiet["day_blink"] or quiet["ip_blink"] or quiet["flag_blink"])
        _Cursor.quota[:] = [{"client_ip": "*", "read_count": 240}, {"client_ip": "1.1.1.1", "read_count": 20},
                            {"client_ip": "2.2.2.2", "read_count": 16}, {"client_ip": "3.3.3.3", "read_count": 3}]
        _Cursor.flags[:] = [{"id": 1}]
        got = self.oc.indicators(_Cursor(), 20, 300)
        self.assertEqual((got["reads_today"], got["at_limit"], got["near_limit"], got["open_flags"]), (240, 1, 1, 1))
        self.assertTrue(got["day_blink"] and got["ip_blink"] and got["flag_blink"])
        self.assertFalse(self.oc.indicators(_Bad(), 20, 300)["day_blink"])


class Routes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Blueprint, Flask
        pkg_name = "occ_pkg"
        pkg = types.ModuleType(pkg_name); pkg.__path__ = [REPO]; sys.modules[pkg_name] = pkg
        cls.auth = [True]

        class DB:
            def cursor(self):
                return _Cursor()

            def commit(self):
                pass

            def rollback(self):
                pass
        db = types.ModuleType(pkg_name + ".db"); db.get_db = lambda: DB(); sys.modules[db.__name__] = db; pkg.db = db
        ops = types.ModuleType(pkg_name + ".ops"); ops._require_ops_auth = lambda: cls.auth[0]
        sys.modules[ops.__name__] = ops; pkg.ops = ops
        spec = importlib.util.spec_from_file_location(pkg_name + ".ops_controls", os.path.join(REPO, "ops_controls.py"))
        mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)
        cls.oc = mod
        cls.tmp = tempfile.mkdtemp(prefix="occ")
        shutil.copy(os.path.join(REPO, "templates", "ops_controls.html"), cls.tmp)
        cls.app = Flask(__name__, template_folder=cls.tmp)
        cls.app.config.update(TESTING=True, SECRET_KEY="t")
        bp = Blueprint("main", __name__)
        mod.register(bp)
        cls.app.register_blueprint(bp)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        _reset()
        self.auth[0] = True

    def _set(self, **body):
        return self.client.post("/api/ops/controls", json=body, headers={"X-Ops-Operator": "Asha"})

    def test_every_route_fails_closed(self):
        self.auth[0] = False
        self.assertEqual(self.client.get("/ops/controls").status_code, 401)
        self.assertEqual(self.client.get("/api/ops/controls").status_code, 401)
        self.assertEqual(self._set(key=self.oc.NCR24_FEE, value=1200).status_code, 401)
        self.assertEqual(self.client.post("/api/ops/controls/flags/1/review").status_code, 401)
        self.assertEqual(_Cursor.store, {})

    def test_increase_decrease_and_set(self):
        self.assertEqual(self._set(key=self.oc.NCR24_FEE, delta=50).get_json()["value"], 1050)
        self.assertEqual(self._set(key=self.oc.NCR24_FEE, delta=-100).get_json()["value"], 950)
        self.assertEqual(self._set(key=self.oc.RX_PER_IP, value="25").get_json()["value"], 25)
        self.assertEqual(self._set(key=self.oc.RX_PER_IP, value=0).status_code, 400)
        self.assertEqual(self._set(key="nope", value=1).status_code, 400)
        self.assertEqual(self.client.post("/api/ops/controls", json={"key": self.oc.NCR24_FEE, "value": 1200}).status_code, 400)
        self.assertEqual(len(_Cursor.log), 3)

    def test_two_people_changing_a_value_cannot_lose_a_change(self):
        self._set(key=self.oc.NCR24_FEE, delta=50)
        lock_at = next(i for i, q in enumerate(_Cursor.sqls) if q.endswith("FOR UPDATE"))
        write_at = next(i for i, q in enumerate(_Cursor.sqls) if q.startswith("INSERT INTO ops_settings "))
        self.assertLess(lock_at, write_at)

    def test_the_page_and_json_show_values_and_blink(self):
        _Cursor.quota[:] = [{"client_ip": "*", "read_count": 299}]
        _Cursor.flags[:] = [{"id": 4, "created_at": datetime.datetime(2026, 11, 9, 9, 0), "door": "chat", "category": "sexual",
                             "sha256": "a" * 64, "client_ip": "1.1.1.1", "customer_id": None}]
        j = self.client.get("/api/ops/controls").get_json()
        self.assertEqual([s["value"] for s in j["settings"]], [1000, 20, 300, 1, 1])
        self.assertTrue(j["indicators"]["day_blink"])
        page = self.client.get("/ops/controls")
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(b'class="tile blink"', page.data)
        self.assertIn(b"299 / 300", page.data)
        self.assertIn(b'data-review="4"', page.data)

    def test_the_24_hour_service_can_be_switched_off_and_on(self):
        self.assertEqual(self._set(key=self.oc.NCR24_ENABLED, value="0").get_json()["value"], 0)
        self.assertEqual(self._set(key=self.oc.NCR24_ENABLED, value="2").status_code, 400)
        page = self.client.get("/ops/controls")
        self.assertIn(b"OFF", page.data)
        self.assertIn(b'class="tile blink"', page.data)
        self.assertIn(b'data-next="1"', page.data)
        self.assertEqual(self._set(key=self.oc.NCR24_ENABLED, value="1").get_json()["value"], 1)
        self.assertEqual(len(_Cursor.log), 2)

    def test_a_flag_review_names_the_person(self):
        self.assertEqual(self.client.post("/api/ops/controls/flags/4/review").status_code, 400)
        r = self.client.post("/api/ops/controls/flags/4/review", headers={"X-Ops-Operator": "Asha"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(_Cursor.reviewed[0][1:], ("Asha", 4))


if __name__ == "__main__":
    unittest.main()
