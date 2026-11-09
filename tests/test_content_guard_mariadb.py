"""The upload check's SQL on a real MariaDB, starting from the table as it went
live (no checked_by). TEMPORARY tables, so nothing outlives the connection."""
import datetime
import importlib.util
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_CONF = dict(
    host=os.environ.get("OPTIWAR_TEST_MYSQL_HOST", "127.0.0.1"),
    user=os.environ.get("OPTIWAR_TEST_MYSQL_USER", "oslb6"),
    password=os.environ.get("OPTIWAR_TEST_MYSQL_PASSWORD", "testpw"),
    database=os.environ.get("OPTIWAR_TEST_MYSQL_DB", "optiwar2"),
)


def _connect():
    import pymysql
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, autocommit=True, connect_timeout=5, **DB_CONF)


def _available():
    try:
        _connect().close()
        return True
    except Exception:  # noqa: BLE001 - any failure means "no database here"
        return False


def _load(name):
    spec = importlib.util.spec_from_file_location(name + "_mariadb", os.path.join(REPO, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(_available(), "no MariaDB test database (see scripts/setup_test_db.sh)")
class WhichAiOnRealMariaDB(unittest.TestCase):

    def setUp(self):
        self.cg, self.ops = _load("content_guard"), _load("ops_controls")
        self.db = _connect()
        self.addCleanup(self.db.close)
        self.cur = self.db.cursor()
        temporary = "CREATE TEMPORARY TABLE"
        live = re.sub(r"\s*checked_by VARCHAR\(24\) NULL,", "", self.cg.SCHEMA)
        self.cur.execute(live.replace("CREATE TABLE IF NOT EXISTS", temporary))
        self.cur.execute(self.cg.COUNTS_SCHEMA.replace("CREATE TABLE IF NOT EXISTS", temporary))

    def _add_column(self):
        self.cg._SCHEMA_READY = False
        real = self.cg.TABLES
        self.cg.TABLES = ()
        self.addCleanup(setattr, self.cg, "TABLES", real)
        self.cg.ensure_schema(self.cur)

    def test_a_flag_is_kept_and_shown_before_the_column_is_added(self):
        self.cg.flag(self.cur, "sexual", "h1", "198.51.100.1", None, "chat", checked_by="Claude")
        rows = self.ops.open_flags(self.cur)
        self.assertEqual(rows[0]["category"], "sexual")
        self.assertNotIn("checked_by", rows[0])

    def test_the_column_is_added_once_and_shows_which_ai(self):
        self._add_column()
        self._add_column()
        self.cg.flag(self.cur, "extremist", "h2", "198.51.100.1", None, "chat", checked_by="DeepSeek")
        self.assertEqual(self.ops.open_flags(self.cur)[0]["checked_by"], "DeepSeek")

    def test_todays_counts_and_the_blink(self):
        today = datetime.date.today()
        for provider in ("Claude", "Claude"):
            self.cg.count_check(self.db, self.cur, provider, today)
        self.assertEqual(self.ops.ai_checks(self.cur, today), [{"provider": "Claude", "checks": 2}])
        self.assertFalse(self.ops.indicators(self.cur, 20, 300, today)["checks_blink"])
        self.cg.count_check(self.db, self.cur, "OpenAI", today)
        self.assertTrue(self.ops.indicators(self.cur, 20, 300, today)["checks_blink"])
