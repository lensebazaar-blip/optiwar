"""lens_rx: a paid contact-lens order carries the prescription it was placed with.

Unit tests need no database. The ``OnMariaDB`` cases run against the CI
MariaDB (OPTIWAR_TEST_MYSQL_*) and are skipped without one; they prove the
idempotency and the race the unit tests can only describe.
"""
import datetime
import importlib.util
import os
import sys
import threading
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


lens_order = _load("lens_order")
lens_rx = _load("lens_rx")
contact_lens = _load("contact_lens")


def _item(right_boxes=6, left_boxes=6, product_id=1015, **extra):
    item = {
        "product_id": product_id, "product_name": "Precision1 (30 pack)",
        "product_category": "Contact Lenses", "vertical": "CONTACT_LENS",
        "product_special_price": 15.11, "rx_id": None,
        "right_qty": right_boxes, "right_pwr": "-3.75", "right_cyl": "",
        "right_axis": "", "right_add": "", "right_bc": "8.3",
        "right_dia": "14.2", "right_lens_color": "", "right_variant_id": 41,
        "left_qty": left_boxes, "left_pwr": "-4.25", "left_cyl": "-0.75",
        "left_axis": "180", "left_add": "", "left_bc": "8.3",
        "left_dia": "14.2", "left_lens_color": "", "left_variant_id": 57,
    }
    item.update(extra)
    return item


class TypedEyes(unittest.TestCase):
    def test_each_eye_is_read_as_typed_values(self):
        eyes = lens_rx.eyes_from_item(_item())
        self.assertEqual(eyes["right"]["sph"], -3.75)
        self.assertIsNone(eyes["right"]["cyl"])
        self.assertEqual(eyes["right"]["base_curve"], 8.3)
        self.assertEqual(eyes["right"]["diameter"], 14.2)
        self.assertEqual(eyes["right"]["variant_id"], 41)
        self.assertEqual(eyes["right"]["boxes"], 6)
        self.assertEqual(eyes["left"]["cyl"], -0.75)
        self.assertEqual(eyes["left"]["axis"], 180)

    def test_an_eye_without_boxes_holds_no_values(self):
        eyes = lens_rx.eyes_from_item(_item(left_boxes=0))
        self.assertEqual(eyes["left"]["boxes"], 0)
        self.assertTrue(all(v is None for k, v in eyes["left"].items()
                            if k != "boxes"))

    def test_the_cart_item_states_the_diameter(self):
        variant = {"variant_id": 1, "sph": "-4.50", "cyl": "", "axis": "",
                   "add_power": "", "base_curve": "8.3", "diameter": "14.2",
                   "color_code": "", "availability": "IN_STOCK"}
        product = {"product_id": 1, "product_name": "L", "product_code": "L",
                   "product_category": "Contact Lenses",
                   "product_special_price": 10, "product_price": 10,
                   "image_url": "", "vertical": "CONTACT_LENS"}
        item = lens_order.cart_item(product, [
            {"eye": "right", "variant": variant, "boxes": 6}])
        self.assertEqual(item["right_dia"], "14.20")


class LegacyString(unittest.TestCase):
    def test_the_legacy_string_keeps_pwr_cyl_qty_color_in_place(self):
        s = lens_rx.legacy_eye_string(lens_rx.eyes_from_item(_item())["left"])
        parts = s.split("/")
        self.assertEqual(parts[:4], ["-4.25", "-0.75", "6", ""])
        self.assertEqual(parts[4], "180")

    def test_an_unordered_eye_is_the_sentinel_the_templates_know(self):
        eyes = lens_rx.eyes_from_item(_item(left_boxes=0))
        self.assertEqual(lens_rx.legacy_eye_string(eyes["left"]),
                         "No RX selected")


class Retention(unittest.TestCase):
    def test_twenty_four_months_after_creation(self):
        self.assertEqual(
            lens_rx.retain_until(datetime.datetime(2026, 9, 7, 13, 0)),
            datetime.date(2028, 9, 7))
        self.assertEqual(
            lens_rx.retain_until(datetime.datetime(2028, 2, 29)),
            datetime.date(2030, 2, 28))


class LineDetection(unittest.TestCase):
    def test_only_lens_lines_get_a_snapshot(self):
        self.assertTrue(lens_rx.is_lens_line(_item()))
        self.assertTrue(lens_rx.is_lens_line(
            {"product_category": "Contact Lenses"}))
        self.assertFalse(lens_rx.is_lens_line(
            {"product_category": "Eyeglasses", "vertical": "EYEWEAR"}))
        self.assertFalse(lens_rx.is_lens_line(None))

    def test_the_schema_is_declared_where_the_deploy_tool_looks(self):
        self.assertIn(lens_rx.TABLE, contact_lens.TABLES)
        self.assertIn("UNIQUE KEY uq_clrx_line (order_id, product_id)",
                      lens_rx.SCHEMA)

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(ValueError):
            lens_rx.record(None, _item(), 1, "X", "optiwar.com",
                           source="GUESSED")


class CheckoutWiring(unittest.TestCase):
    """models.py is not importable without Flask + a database; read it."""

    def test_every_order_line_insert_is_preceded_by_a_lens_snapshot(self):
        with open(os.path.join(REPO, "models.py")) as fh:
            src = fh.read()
        self.assertIn("lens_rx", src.split("\n\n")[0] + src[:2000])
        inserts = [i for i in range(len(src))
                   if src.startswith("INSERT INTO orders (order_id", i)]
        self.assertGreaterEqual(len(inserts), 2)
        for pos in inserts:
            window = src[max(0, pos - 3000):pos]
            self.assertIn("lens_rx.record(cursor, item, customer_id, order_id",
                          window)
            self.assertIn("if not rx_id and lens_rx.is_lens_line(item)", window)


class _Recorder(object):
    def __init__(self):
        self.statements, self.lastrowid = [], 4242

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchone(self):
        return None


class RecordedStatements(unittest.TestCase):
    def test_two_rows_one_legacy_one_canonical_in_the_callers_transaction(self):
        cur = _Recorder()
        rx_id = lens_rx.record(cur, _item(), 77, "ORD1", "optiwar.com",
                               now=datetime.datetime(2026, 9, 7))
        self.assertEqual(rx_id, 4242)
        sqls = [s for s, _ in cur.statements]
        self.assertTrue(sqls[0].startswith("SELECT GET_LOCK"))
        self.assertTrue(sqls[1].startswith("SELECT rx_id FROM contact_lens"))
        self.assertTrue(sqls[1].endswith("FOR UPDATE"))
        self.assertTrue(sqls[2].startswith("INSERT INTO rx_collector"))
        self.assertTrue(sqls[3].startswith(
            "INSERT INTO contact_lens_prescriptions"))
        self.assertTrue(sqls[4].startswith("SELECT RELEASE_LOCK"))
        self.assertFalse(any(s.upper().startswith(("COMMIT", "BEGIN"))
                             for s in sqls))
        cols, vals = cur.statements[3][0], cur.statements[3][1]
        row = dict(zip(cols[cols.index("(") + 1:cols.index(")")].split(", "),
                       vals))
        self.assertEqual(row["customer_id"], 77)
        self.assertEqual(row["order_id"], "ORD1")
        self.assertEqual(row["rx_type"], "contact_lens")
        self.assertEqual(row["source"], "MANUAL")
        self.assertEqual(row["retain_until"], datetime.date(2028, 9, 7))
        self.assertEqual(row["right_sph"], -3.75)
        self.assertEqual(row["left_axis"], 180)
        self.assertEqual(row["left_boxes"], 6)
        self.assertIsNone(row["document_id"])


# --------------------------------------------------------------------------
# Against MariaDB

DB_CONF = dict(
    host=os.environ.get("OPTIWAR_TEST_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("OPTIWAR_TEST_MYSQL_PORT", "3306")),
    user=os.environ.get("OPTIWAR_TEST_MYSQL_USER", "oslb6"),
    password=os.environ.get("OPTIWAR_TEST_MYSQL_PASSWORD", "testpw"),
    database=os.environ.get("OPTIWAR_TEST_MYSQL_DB", "optiwar2"),
)

RX_COLLECTOR = """CREATE TABLE IF NOT EXISTS rx_collector (
    rx_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    recommendations VARCHAR(255) NULL,
    recommendation_price INT NULL,
    right_eye VARCHAR(255) NULL,
    left_eye VARCHAR(255) NULL,
    product_id INT NULL,
    date_created DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def _connect():
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError:
        return None
    try:
        return pymysql.connect(cursorclass=DictCursor, autocommit=False,
                               **DB_CONF)
    except Exception:  # noqa: BLE001 - no database here
        return None


class OnMariaDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = _connect()
        if cls.db is None:
            raise unittest.SkipTest("no MariaDB (OPTIWAR_TEST_MYSQL_*)")
        with cls.db.cursor() as cur:
            cur.execute(RX_COLLECTOR)
            cur.execute(lens_rx.SCHEMA)
        cls.db.commit()

    @classmethod
    def tearDownClass(cls):
        if cls.db is not None:
            cls.db.close()

    def setUp(self):
        self.order_id = "T" + uuid.uuid4().hex[:12]

    def _rows(self):
        self.db.commit()  # a fresh snapshot: see what other connections wrote
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM contact_lens_prescriptions "
                        "WHERE order_id=%s", (self.order_id,))
            rows = cur.fetchall()
        self.db.commit()
        return rows

    def test_the_snapshot_is_readable_back_typed_and_owned(self):
        with self.db.cursor() as cur:
            rx_id = lens_rx.record(cur, _item(), 501, self.order_id,
                                   "optiwar.com")
            cur.execute("SELECT right_eye, left_eye FROM rx_collector "
                        "WHERE rx_id=%s", (rx_id,))
            legacy = cur.fetchone()
        self.db.commit()
        self.assertTrue(legacy["right_eye"].startswith("-3.75//6/"))
        self.assertTrue(legacy["left_eye"].startswith("-4.25/-0.75/6//180"))
        with self.db.cursor() as cur:
            (row,) = lens_rx.for_order(cur, self.order_id)
            mine = lens_rx.for_customer(cur, 501)
            theirs = lens_rx.for_customer(cur, 502)
        self.assertEqual(row["rx_id"], rx_id)
        self.assertEqual(float(row["right_sph"]), -3.75)
        self.assertEqual(row["left_axis"], 180)
        self.assertEqual(row["right_boxes"], 6)
        self.assertEqual(row["retain_until"],
                         lens_rx.retain_until(row["created_at"]))
        self.assertIn(rx_id, [r["rx_id"] for r in mine])
        self.assertNotIn(rx_id, [r["rx_id"] for r in theirs])
        self.assertEqual(lens_rx.describe_row(row)[0][:10], "RIGHT (OD)")

    def test_a_retried_checkout_returns_the_same_snapshot(self):
        with self.db.cursor() as cur:
            first = lens_rx.record(cur, _item(), 501, self.order_id, "optiwar.com")
        self.db.commit()
        with self.db.cursor() as cur:
            again = lens_rx.record(cur, _item(), 501, self.order_id, "optiwar.com")
        self.db.commit()
        self.assertEqual(first, again)
        self.assertEqual(len(self._rows()), 1)

    def test_a_rolled_back_order_leaves_no_prescription(self):
        with self.db.cursor() as cur:
            rx_id = lens_rx.record(cur, _item(), 501, self.order_id, "optiwar.com")
        self.db.rollback()
        self.assertEqual(self._rows(), ())
        with self.db.cursor() as cur:
            cur.execute("SELECT 1 FROM rx_collector WHERE rx_id=%s", (rx_id,))
            self.assertIsNone(cur.fetchone())

    def test_two_lens_lines_on_one_order_are_two_snapshots(self):
        with self.db.cursor() as cur:
            a = lens_rx.record(cur, _item(product_id=1015), 501,
                               self.order_id, "optiwar.com")
            b = lens_rx.record(cur, _item(product_id=1016), 501,
                               self.order_id, "optiwar.com")
        self.db.commit()
        self.assertNotEqual(a, b)
        self.assertEqual(len(self._rows()), 2)

    def test_concurrent_submits_of_one_order_line_yield_one_snapshot(self):
        """N connections each begin, record, commit for the same order line.

        The unique key makes the losers block on the winner's insert, then
        take the winner's rx_id; nobody duplicates and nobody errors.
        """
        results, errors = [], []
        gate = threading.Barrier(6)

        def worker():
            conn = _connect()
            try:
                gate.wait(timeout=10)
                with conn.cursor() as cur:
                    rx = lens_rx.record(cur, _item(), 501, self.order_id,
                                        "optiwar.com")
                conn.commit()
                results.append(rx)
            except Exception as exc:  # noqa: BLE001 - reported below
                conn.rollback()
                errors.append(repr(exc))
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        self.assertEqual(len(set(results)), 1)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        with self.db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM rx_collector r "
                        "JOIN contact_lens_prescriptions c ON c.rx_id=r.rx_id "
                        "WHERE c.order_id=%s", (self.order_id,))
            self.assertEqual(cur.fetchone()["n"], 1)
        self.db.commit()


if __name__ == "__main__":
    unittest.main()
