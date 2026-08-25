"""The paid-order pipeline, evaluated against a real database.

The questions worth asking about money are all about what happens *twice*, and
a fake cursor cannot answer any of them: a UNIQUE key is what makes a retried
webhook harmless, a conditional UPDATE is what makes two simultaneous payments
for the last frame produce one sale and one refund, and only rows can show that
a payment arriving after dispatch left the ``Shipped`` row alone.

Skipped, not failed, when no test database is reachable.

    scripts/setup_test_db.sh    # creates optiwar2_pipeline
    python3 -m unittest tests.test_paid_order_pipeline
"""
import importlib.util
import os
import sys
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


paid_orders = _load("paid_orders")

# A database of its own, created by scripts/setup_test_db.sh: production's
# orders table has one row per line item keyed by order_line_id, which cannot
# coexist with the one-row-per-order fake the attribution tests create under the
# same name.
_MAIN_DB = os.environ.get("OPTIWAR_TEST_MYSQL_DB", "optiwar2")

DB_CONF = dict(
    host=os.environ.get("OPTIWAR_TEST_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("OPTIWAR_TEST_MYSQL_PORT", "3306")),
    user=os.environ.get("OPTIWAR_TEST_MYSQL_USER", "oslb6"),
    password=os.environ.get("OPTIWAR_TEST_MYSQL_PASSWORD", "testpw"),
    database=os.environ.get("OPTIWAR_TEST_PIPELINE_DB", _MAIN_DB + "_pipeline"),
)

# Only the columns the pipeline touches, shaped as production has them. Created
# when absent and never altered, so pointing OPTIWAR_TEST_PIPELINE_DB at a
# database that already has the real tables exercises the real ones.
DDL = [
    """CREATE TABLE IF NOT EXISTS orders (
        order_line_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_id           VARCHAR(64) NOT NULL,
        customer_id        BIGINT NULL,
        address_id         BIGINT NULL,
        product_id         BIGINT NULL,
        order_quantity     INT NOT NULL DEFAULT 1,
        order_total        INT NOT NULL DEFAULT 0,
        fulfillment_status ENUM('pending','fulfilled','refund_pending') NOT NULL DEFAULT 'pending',
        is_test            TINYINT NOT NULL DEFAULT 0,
        KEY idx_order (order_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS order_status (
        order_status_id   BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_status_name VARCHAR(32) NOT NULL,
        order_id          VARCHAR(64) NOT NULL,
        source            VARCHAR(32) NULL,
        manual_flag       TINYINT NOT NULL DEFAULT 0,
        note              VARCHAR(255) NULL,
        created_at        DATETIME NULL,
        KEY idx_order (order_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS order_history (
        order_history_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_history_content TEXT NULL,
        order_id              VARCHAR(64) NOT NULL,
        site_from             VARCHAR(64) NULL,
        order_history_date    DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_order (order_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS payment_collector (
        id           BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_id     VARCHAR(64) NOT NULL,
        payment_ref  VARCHAR(191) NULL,
        payment_dump LONGTEXT NOT NULL,
        status       VARCHAR(32) NULL,
        date_created DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_payment_ref (payment_ref),
        KEY idx_order (order_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS products (
        product_id        BIGINT AUTO_INCREMENT PRIMARY KEY,
        product_code      VARCHAR(32) NULL,
        product_quantity  INT NOT NULL DEFAULT 0,
        product_status    VARCHAR(24) NULL,
        status_changed_at DATETIME NULL,
        status_changed_by VARCHAR(100) NULL,
        status_reason     VARCHAR(255) NULL,
        sold_out_at       DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS sales_log (
        id         BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_id   VARCHAR(64) NOT NULL,
        product_id BIGINT NULL,
        qty        INT NULL,
        unit_price DECIMAL(12,2) NULL,
        currency   VARCHAR(8) NULL,
        site       VARCHAR(64) NULL,
        is_test    TINYINT NOT NULL DEFAULT 0,
        KEY idx_order (order_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS product_status_history (
        id         BIGINT AUTO_INCREMENT PRIMARY KEY,
        product_id BIGINT NULL,
        old_status VARCHAR(24) NULL,
        new_status VARCHAR(24) NULL,
        reason     VARCHAR(255) NULL,
        changed_by VARCHAR(100) NULL,
        changed_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


def _connect():
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError:
        return None
    try:
        return pymysql.connect(cursorclass=DictCursor, autocommit=False, **DB_CONF)
    except Exception:
        return None


class PaidOrderPipelineTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = _connect()
        if cls.db is None:
            raise unittest.SkipTest("no test database reachable")
        cur = cls.db.cursor()
        for stmt in DDL:
            cur.execute(stmt)
        cls.db.commit()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "db", None) is not None:
            cls.db.close()

    def setUp(self):
        self.cur = self.db.cursor()
        self._orders = []
        self._products = []

    def tearDown(self):
        for order_id in self._orders:
            for table in ("sales_log", "payment_collector", "order_history",
                          "order_status", "orders"):
                self.cur.execute("DELETE FROM %s WHERE order_id=%%s" % table, (order_id,))
        for pid in self._products:
            self.cur.execute("DELETE FROM product_status_history WHERE product_id=%s", (pid,))
            self.cur.execute("DELETE FROM products WHERE product_id=%s", (pid,))
        self.db.commit()

    # ─── fixtures ────────────────────────────────────────────────────

    def _product(self, quantity, status='ACTIVE'):
        code = uuid.uuid4().hex[:8].upper()
        self.cur.execute(
            "INSERT INTO products (product_code, product_quantity, product_status) "
            "VALUES (%s, %s, %s)", (code, quantity, status))
        pid = self.cur.lastrowid
        self._products.append(pid)
        return pid

    def _order(self, lines, status='Pending', is_test=0):
        order_id = uuid.uuid4().hex[:6].upper() + "-TEST"
        self._orders.append(order_id)
        for pid, qty, total in lines:
            self.cur.execute(
                "INSERT INTO orders (order_id, product_id, order_quantity, order_total, "
                "fulfillment_status, is_test) VALUES (%s, %s, %s, %s, 'pending', %s)",
                (order_id, pid, qty, total, is_test))
        for name in ([status] if isinstance(status, str) else status):
            self.cur.execute(
                "INSERT INTO order_status (order_status_name, order_id) VALUES (%s, %s)",
                (name, order_id))
        self.db.commit()
        return order_id

    def _statuses(self, order_id):
        self.cur.execute(
            "SELECT order_status_name FROM order_status WHERE order_id=%s "
            "ORDER BY order_status_id", (order_id,))
        return [r['order_status_name'] for r in self.cur.fetchall()]

    def _stock(self, pid):
        self.cur.execute("SELECT product_quantity FROM products WHERE product_id=%s", (pid,))
        return self.cur.fetchone()['product_quantity']

    def _fulfillment(self, order_id):
        self.cur.execute(
            "SELECT fulfillment_status FROM orders WHERE order_id=%s ORDER BY order_line_id",
            (order_id,))
        return [r['fulfillment_status'] for r in self.cur.fetchall()]

    def _sales_rows(self, order_id):
        self.cur.execute("SELECT * FROM sales_log WHERE order_id=%s", (order_id,))
        return self.cur.fetchall()

    def _apply(self, order_id, ref, **kw):
        kw.setdefault('currency', 'INR')
        kw.setdefault('site', 'in.optiwar.com')
        return paid_orders.apply_paid_order(
            self.db, order_id, ref, {'gateway': 'razorpay', 'ref': ref}, **kw)

    # ─── the pipeline ────────────────────────────────────────────────

    def test_payment_deducts_stock_logs_the_sale_and_appends_processed(self):
        pid = self._product(3)
        order_id = self._order([(pid, 2, 998)])

        result = self._apply(order_id, 'pay_' + uuid.uuid4().hex[:12])

        self.assertTrue(result['applied'])
        self.assertEqual(1, result['fulfilled_count'])
        self.assertEqual([], result['refund_lines'])
        self.assertEqual(1, self._stock(pid))
        self.assertEqual(['fulfilled'], self._fulfillment(order_id))
        sales = self._sales_rows(order_id)
        self.assertEqual(1, len(sales))
        self.assertEqual(2, sales[0]['qty'])
        self.assertEqual('INR', sales[0]['currency'])
        # The Pending row it started with is still there: status is history.
        self.assertEqual(['Pending', 'Processed'], self._statuses(order_id))

    def test_repeated_callback_applies_once(self):
        pid = self._product(5)
        order_id = self._order([(pid, 1, 499)])
        ref = 'pay_' + uuid.uuid4().hex[:12]

        first = self._apply(order_id, ref)
        second = self._apply(order_id, ref)

        self.assertTrue(first['applied'])
        self.assertFalse(second['applied'])
        self.assertEqual('duplicate_payment', second['reason'])
        self.assertEqual(4, self._stock(pid))
        self.assertEqual(1, len(self._sales_rows(order_id)))
        self.assertEqual(['Pending', 'Processed'], self._statuses(order_id))
        self.cur.execute(
            "SELECT COUNT(*) AS n FROM payment_collector WHERE order_id=%s", (order_id,))
        self.assertEqual(1, self.cur.fetchone()['n'])

    def test_late_payment_does_not_walk_a_shipped_order_backwards(self):
        pid = self._product(4)
        order_id = self._order([(pid, 1, 499)], status=['Pending', 'Shipped'])

        result = self._apply(order_id, 'pay_' + uuid.uuid4().hex[:12])

        self.assertTrue(result['applied'])
        self.assertFalse(result['status_appended'])
        self.assertEqual(['Pending', 'Shipped'], self._statuses(order_id))

    def test_oversold_line_becomes_refund_pending_and_is_not_sold(self):
        pid = self._product(1)
        order_id = self._order([(pid, 3, 1497)])

        result = self._apply(order_id, 'pay_' + uuid.uuid4().hex[:12])

        self.assertEqual(0, result['fulfilled_count'])
        self.assertEqual(1, len(result['refund_lines']))
        self.assertEqual(1, self._stock(pid))
        self.assertEqual(['refund_pending'], self._fulfillment(order_id))
        self.assertEqual(0, len(self._sales_rows(order_id)))

    def test_partly_available_order_splits_per_line(self):
        good = self._product(5)
        short = self._product(0)
        order_id = self._order([(good, 1, 499), (short, 1, 499)])

        result = self._apply(order_id, 'pay_' + uuid.uuid4().hex[:12])

        self.assertEqual(1, result['fulfilled_count'])
        self.assertEqual(1, len(result['refund_lines']))
        self.assertEqual(['fulfilled', 'refund_pending'], self._fulfillment(order_id))
        self.assertEqual(1, len(self._sales_rows(order_id)))

    def test_depleting_stock_marks_the_product_out_of_stock_once(self):
        pid = self._product(2)
        order_id = self._order([(pid, 2, 998)])

        self._apply(order_id, 'pay_' + uuid.uuid4().hex[:12])

        self.cur.execute(
            "SELECT product_status, sold_out_at FROM products WHERE product_id=%s", (pid,))
        row = self.cur.fetchone()
        self.assertEqual('OUT_OF_STOCK', row['product_status'])
        self.assertIsNotNone(row['sold_out_at'])
        self.cur.execute(
            "SELECT COUNT(*) AS n FROM product_status_history WHERE product_id=%s", (pid,))
        self.assertEqual(1, self.cur.fetchone()['n'])

    def test_a_manual_product_state_survives_the_sale(self):
        pid = self._product(1, status='DISCONTINUED')
        order_id = self._order([(pid, 1, 499)])

        self._apply(order_id, 'pay_' + uuid.uuid4().hex[:12])

        self.cur.execute("SELECT product_status FROM products WHERE product_id=%s", (pid,))
        self.assertEqual('DISCONTINUED', self.cur.fetchone()['product_status'])

    def test_test_orders_take_no_stock_and_log_no_sale(self):
        pid = self._product(3)
        order_id = self._order([(pid, 1, 499)], is_test=1)

        result = self._apply(order_id, 'pay_' + uuid.uuid4().hex[:12])

        self.assertTrue(result['is_test'])
        self.assertEqual(3, self._stock(pid))
        self.assertEqual(0, len(self._sales_rows(order_id)))
        self.assertEqual(['Pending', 'Processed'], self._statuses(order_id))

    def test_amount_owed_is_reported_in_minor_units(self):
        pid = self._product(9)
        order_id = self._order([(pid, 1, 499), (pid, 2, 998)])
        self.assertEqual(149700, paid_orders.order_amount_minor(self.cur, order_id))

    def test_the_appended_status_carries_its_provenance(self):
        pid = self._product(2)
        order_id = self._order([(pid, 1, 499)])
        ref = 'pay_' + uuid.uuid4().hex[:12]

        self._apply(order_id, ref, source='razorpay-webhook')

        self.cur.execute(
            "SELECT source, note FROM order_status WHERE order_id=%s "
            "AND order_status_name='Processed'", (order_id,))
        row = self.cur.fetchone()
        self.assertEqual('razorpay-webhook', row['source'])
        self.assertIn(ref, row['note'])

    def test_history_records_the_payment(self):
        pid = self._product(2)
        order_id = self._order([(pid, 1, 499)])
        ref = 'pay_' + uuid.uuid4().hex[:12]

        self._apply(order_id, ref)

        self.cur.execute(
            "SELECT order_history_content FROM order_history WHERE order_id=%s", (order_id,))
        contents = [r['order_history_content'] for r in self.cur.fetchall()]
        self.assertTrue(any(ref in c for c in contents), contents)


if __name__ == '__main__':
    unittest.main()
