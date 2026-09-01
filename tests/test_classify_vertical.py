"""Classifying a product must be reversible, explicable, and nothing else.

The script's job is two columns and a vertical. What it must not do is anything
else: a lens is not written off by being classified, an ACTIVE frame stays
ACTIVE, and a product removed from a storefront leaves a record saying who did
it. Each is a test, against a real database where one is available.

    OPTIWAR_TEST_MYSQL_DB=optiwar2 python3 -m unittest tests.test_classify_vertical
"""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_CONF = dict(
    host=os.environ.get("OPTIWAR_TEST_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("OPTIWAR_TEST_MYSQL_PORT", "3306")),
    user=os.environ.get("OPTIWAR_TEST_MYSQL_USER", "oslb6"),
    password=os.environ.get("OPTIWAR_TEST_MYSQL_PASSWORD", "testpw"),
    database=os.environ.get("OPTIWAR_TEST_MYSQL_DB", "optiwar2"),
)

PRODUCTS_DDL = """
CREATE TABLE IF NOT EXISTS products (
    product_id       INT NOT NULL PRIMARY KEY,
    product_code     VARCHAR(40) NOT NULL,
    product_name     VARCHAR(200) NOT NULL,
    product_category VARCHAR(80) NOT NULL,
    product_status   VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    product_quantity INT NOT NULL DEFAULT 0,
    show_in_listings TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB
"""

HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS product_status_history (
    id         INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    product_id INT NOT NULL,
    old_status VARCHAR(20) NULL,
    new_status VARCHAR(20) NULL,
    reason     VARCHAR(255) NULL,
    changed_by VARCHAR(80) NULL,
    changed_at DATETIME NULL
) ENGINE=InnoDB
"""


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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

script = _load("classify_vertical_under_test",
               os.path.join(REPO, "scripts", "classify_vertical.py"))
contact_lens = _load("contact_lens_for_classify",
                     os.path.join(REPO, "contact_lens.py"))


class TargetTest(unittest.TestCase):
    def test_a_lens_defaults_to_com_only(self):
        self.assertEqual(script.target("CONTACT_LENS", None),
                         {"product_vertical": "CONTACT_LENS",
                          "sell_on_com": 1, "sell_on_in": 0})

    def test_eyewear_defaults_to_both_storefronts(self):
        self.assertEqual(script.target("EYEWEAR", None),
                         {"product_vertical": "EYEWEAR",
                          "sell_on_com": 1, "sell_on_in": 1})

    def test_an_explicit_sell_on_overrides_the_default(self):
        self.assertEqual(script.target("CONTACT_LENS", ["in"]),
                         {"product_vertical": "CONTACT_LENS",
                          "sell_on_com": 0, "sell_on_in": 1})

    def test_a_product_sold_nowhere_is_expressible(self):
        # Withdrawing a product from both storefronts without touching its
        # lifecycle is a real request; it must not require an empty --sell-on to
        # silently mean "both".
        self.assertEqual(script.target("EYEWEAR", []),
                         {"product_vertical": "EYEWEAR",
                          "sell_on_com": 1, "sell_on_in": 1})
        self.assertEqual(script.target("EYEWEAR", ["com"])["sell_on_in"], 0)

    def test_rerunning_the_same_classification_changes_nothing(self):
        want = script.target("CONTACT_LENS", None)
        row = {"product_vertical": "CONTACT_LENS", "sell_on_com": 1,
               "sell_on_in": 0}
        self.assertFalse(script.needs_change(row, want))
        self.assertTrue(script.needs_change(
            dict(row, sell_on_in=1), want))


@unittest.skipUnless(AVAILABLE, "no MariaDB available")
class ApplyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = _connect()
        cur = cls.db.cursor()
        cur.execute("DROP TABLE IF EXISTS contact_lens_variants")
        cur.execute("DROP TABLE IF EXISTS contact_lens_param_rules")
        cur.execute("DROP TABLE IF EXISTS contact_lens_images")
        cur.execute("DROP TABLE IF EXISTS contact_lens_products")
        cur.execute("DROP TABLE IF EXISTS products")
        cur.execute("DROP TABLE IF EXISTS product_status_history")
        cur.execute(PRODUCTS_DDL)
        cur.execute(HISTORY_DDL)
        cur.executemany(
            "INSERT INTO products (product_id, product_code, product_name,"
            " product_category, product_status, product_quantity)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            [(1, "BB44", "Optiwar Baby Frame", "Spectacles Frame", "ACTIVE", 5),
             (1005, "CL-ADCL", "Aryan 1-Day Color Contact Lens",
              "Contact Lenses", "OUT_OF_STOCK", 0)])
        contact_lens._SCHEMA_READY = False
        contact_lens.ensure_schema(cur)
        cls.db.commit()

    @classmethod
    def tearDownClass(cls):
        cur = cls.db.cursor()
        cur.execute("DROP TABLE IF EXISTS contact_lens_variants")
        cur.execute("DROP TABLE IF EXISTS contact_lens_param_rules")
        cur.execute("DROP TABLE IF EXISTS contact_lens_images")
        cur.execute("DROP TABLE IF EXISTS contact_lens_products")
        cur.execute("DROP TABLE IF EXISTS products")
        cur.execute("DROP TABLE IF EXISTS product_status_history")
        cls.db.commit()
        cls.db.close()

    def setUp(self):
        self.cur = self.db.cursor()

    def tearDown(self):
        self.db.rollback()

    def _classify(self, ids, vertical="CONTACT_LENS", sell_on=None,
                  by="tester"):
        want = script.target(vertical, sell_on)
        rows = script.select_products(self.cur, None, ids)
        changing = [r for r in rows if script.needs_change(r, want)]
        reason = "vertical: %s, sites com=%s in=%s" % (
            want["product_vertical"], want["sell_on_com"], want["sell_on_in"])
        for r in changing:
            self.cur.execute(
                "UPDATE products SET product_vertical=%s, sell_on_com=%s,"
                " sell_on_in=%s WHERE product_id=%s",
                (want["product_vertical"], want["sell_on_com"],
                 want["sell_on_in"], r["product_id"]))
            self.cur.execute(
                "INSERT INTO product_status_history (product_id, old_status,"
                " new_status, reason, changed_by, changed_at)"
                " VALUES (%s, %s, %s, %s, %s, NOW())",
                (r["product_id"], r["product_status"], r["product_status"],
                 reason, by))
        return changing

    def test_1005_becomes_a_com_only_contact_lens(self):
        self.assertEqual([r["product_id"] for r in self._classify([1005])],
                         [1005])
        self.cur.execute("SELECT product_vertical, sell_on_com, sell_on_in"
                         " FROM products WHERE product_id = 1005")
        self.assertEqual(self.cur.fetchone(),
                         {"product_vertical": "CONTACT_LENS",
                          "sell_on_com": 1, "sell_on_in": 0})

    def test_classifying_does_not_write_off_the_product(self):
        # The one thing that would turn a catalogue change into a commercial
        # one: 1005 is OUT_OF_STOCK today and its status is not this script's
        # business either way.
        self._classify([1005])
        self.cur.execute("SELECT product_status, product_quantity"
                         " FROM products WHERE product_id = 1005")
        self.assertEqual(self.cur.fetchone(),
                         {"product_status": "OUT_OF_STOCK",
                          "product_quantity": 0})

    def test_the_frame_is_untouched_by_a_lens_classification(self):
        self._classify([1005])
        self.cur.execute("SELECT product_vertical, sell_on_com, sell_on_in,"
                         " product_status, product_quantity"
                         " FROM products WHERE product_id = 1")
        self.assertEqual(self.cur.fetchone(),
                         {"product_vertical": "EYEWEAR", "sell_on_com": 1,
                          "sell_on_in": 1, "product_status": "ACTIVE",
                          "product_quantity": 5})

    def test_it_leaves_a_record_of_who_removed_it_from_a_storefront(self):
        self._classify([1005], by="sudhanshu")
        self.cur.execute(
            "SELECT old_status, new_status, reason, changed_by"
            " FROM product_status_history WHERE product_id = 1005"
            " ORDER BY id DESC LIMIT 1")
        row = self.cur.fetchone()
        self.assertEqual(row["changed_by"], "sudhanshu")
        self.assertEqual(row["old_status"], row["new_status"])
        self.assertIn("in=0", row["reason"])

    def test_the_undo_script_restores_the_prior_values(self):
        rows = script.select_products(self.cur, None, [1005])
        path = os.path.join(os.path.dirname(__file__), "_restore_test.sql")
        try:
            script.restore_script(rows, path)
            with open(path, encoding="utf-8") as fh:
                sql = fh.read()
            self._classify([1005])
            for statement in sql.split(";"):
                if statement.strip().startswith("UPDATE"):
                    self.cur.execute(statement)
            self.cur.execute("SELECT product_vertical, sell_on_in"
                             " FROM products WHERE product_id = 1005")
            self.assertEqual(self.cur.fetchone(),
                             {"product_vertical": "EYEWEAR", "sell_on_in": 1})
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_a_second_run_finds_nothing_to_do(self):
        self._classify([1005])
        self.assertEqual(self._classify([1005]), [])


if __name__ == "__main__":
    unittest.main()
