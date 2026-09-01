"""One product, two storefronts: what .in may never see or sell.

Phase 1 of the contact-lens work adds a vertical to ``products`` and a
per-storefront flag, and the whole of its value is a negative claim: a contact
lens exists on optiwar.com and does not exist on optiwar.in — not hidden from a
menu, but absent from the listing, the search, the sitemap, the merchant feed,
the product APIs and the model's prompt, and refused by the cart and the product
page. A test per surface, because "we filtered the listing" is exactly how a
vertical leaks through search.

The schema half runs against a real MariaDB when one is available: the variant
matrix depends on NULL behaviour in a UNIQUE index, which a fake cursor cannot
answer and which is the reason uniqueness is carried by a generated column.

    OPTIWAR_TEST_MYSQL_DB=optiwar2 python3 -m unittest \
        tests.test_catalogue_site_eligibility
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

# A cut-down products table: the columns these queries actually read. The real
# one has ~60 more, none of which participate in site eligibility.
PRODUCTS_DDL = """
CREATE TABLE IF NOT EXISTS products (
    product_id       INT NOT NULL PRIMARY KEY,
    product_code     VARCHAR(40) NOT NULL,
    product_name     VARCHAR(200) NOT NULL,
    product_category VARCHAR(80) NOT NULL,
    product_slug     VARCHAR(200) NULL,
    product_status   VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    product_quantity INT NOT NULL DEFAULT 0,
    show_in_listings TINYINT(1) NOT NULL DEFAULT 1
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

catalogue = _load("catalogue_under_test", os.path.join(REPO, "catalogue.py"))
contact_lens = _load("contact_lens_under_test",
                     os.path.join(REPO, "contact_lens.py"))

COM = catalogue.SITE_COM
IN = catalogue.SITE_IN

FRAME = {"product_id": 1, "product_vertical": "EYEWEAR",
         "sell_on_com": 1, "sell_on_in": 1}
LENS = {"product_id": 1005, "product_vertical": "CONTACT_LENS",
        "sell_on_com": 1, "sell_on_in": 0}


class SiteResolutionTest(unittest.TestCase):
    def test_every_india_host_resolves_to_the_india_storefront(self):
        for host in ("optiwar.in", "www.optiwar.in", "in.optiwar.com",
                     "OPTIWAR.IN", "optiwar.in:443"):
            self.assertEqual(catalogue.site_from_host(host), IN, host)

    def test_com_and_anything_unrecognised_resolve_to_com(self):
        # Including the empty host: the fallback must be a real storefront, and
        # .com is the one that sells everything, so an unknown host cannot
        # accidentally become a site with no catalogue at all.
        for host in ("optiwar.com", "www.optiwar.com", "localhost", "", None):
            self.assertEqual(catalogue.site_from_host(host), COM, host)

    def test_outside_a_request_the_site_is_com_not_an_exception(self):
        # Feeds, the importer and cron have no request context; raising here
        # would turn a filter into an outage.
        self.assertEqual(catalogue.current_site(), COM)


class ProductAllowedTest(unittest.TestCase):
    def test_a_lens_is_sellable_on_com_and_not_on_in(self):
        self.assertTrue(catalogue.is_product_allowed(LENS, COM))
        self.assertFalse(catalogue.is_product_allowed(LENS, IN))

    def test_a_frame_is_sellable_on_both(self):
        for site in (COM, IN):
            self.assertTrue(catalogue.is_product_allowed(FRAME, site), site)

    def test_a_row_without_the_columns_falls_back_to_the_vertical(self):
        # A projection that did not select sell_on_* must not become an
        # accidental allow: the vertical still refuses the lens on .in.
        lens = {"product_id": 1005, "product_vertical": "CONTACT_LENS"}
        self.assertFalse(catalogue.is_product_allowed(lens, IN))
        self.assertTrue(catalogue.is_product_allowed(lens, COM))

    def test_a_row_with_neither_column_nor_vertical_stays_sellable(self):
        # What every product looks like before the migration is applied. The
        # release must not empty both storefronts on the way in.
        for site in (COM, IN):
            self.assertTrue(
                catalogue.is_product_allowed({"product_id": 7}, site), site)

    def test_no_product_is_not_allowed_anywhere(self):
        self.assertFalse(catalogue.is_product_allowed(None, COM))

    def test_an_unreadable_flag_refuses_rather_than_guesses(self):
        lens = dict(LENS, sell_on_in="yes")
        self.assertFalse(catalogue.is_product_allowed(lens, IN))

    def test_vertical_defaults_to_eyewear_however_it_is_written(self):
        for raw in (None, "", "  ", "eyewear", "EyeWear"):
            self.assertEqual(
                catalogue.vertical({"product_vertical": raw}), "EYEWEAR", raw)
        self.assertTrue(catalogue.is_contact_lens(
            {"product_vertical": " contact_lens "}))


class SiteFilterTest(unittest.TestCase):
    def test_the_predicate_names_the_column_for_the_site(self):
        self.assertEqual(catalogue.catalogue_site_filter(COM),
                         " AND sell_on_com = 1")
        self.assertEqual(catalogue.catalogue_site_filter(IN),
                         " AND sell_on_in = 1")

    def test_it_can_be_qualified_for_a_joined_query(self):
        self.assertEqual(catalogue.catalogue_site_filter(IN, alias="p"),
                         " AND p.sell_on_in = 1")

    def test_it_carries_no_parameters(self):
        # The read paths build WHERE clauses as f-strings and pass their own
        # parameter tuples; a %s here would shift every one of them.
        self.assertNotIn("%s", catalogue.catalogue_site_filter(IN))


class SitemapStrippingTest(unittest.TestCase):
    XML = ("""<?xml version="1.0" encoding="UTF-8"?>\n<urlset>"""
           "<url><loc>https://optiwar.com/eyeglasses/frame.html</loc></url>"
           "<url><loc>https://optiwar.com/contact_lenses</loc></url>"
           "<url><loc>https://optiwar.com/categories/contact-lenses/aryan"
           "</loc><lastmod>2026-01-01</lastmod></url>"
           "<url><loc>https://optiwar.com/about</loc></url>"
           "</urlset>")

    def test_india_publishes_no_lens_url(self):
        out = catalogue.strip_ineligible_urls(self.XML, IN)
        self.assertNotIn("contact_lenses", out)
        self.assertNotIn("contact-lenses", out)
        self.assertIn("frame.html", out)
        self.assertIn("/about", out)

    def test_com_is_returned_untouched(self):
        self.assertEqual(catalogue.strip_ineligible_urls(self.XML, COM),
                         self.XML)

    def test_a_multiline_entry_is_removed_whole(self):
        # The generated file is pretty-printed; a line-based filter would leave
        # the <loc> behind and produce invalid XML.
        xml = ("<urlset>\n  <url>\n    <loc>https://optiwar.com/contact_lenses"
               "</loc>\n    <priority>0.8</priority>\n  </url>\n</urlset>")
        out = catalogue.strip_ineligible_urls(xml, IN)
        self.assertNotIn("<loc>", out)
        self.assertNotIn("priority", out)


class FakeCursor:
    """Just enough cursor to answer sellable_here()."""

    def __init__(self, rows):
        self.rows = rows
        self.row = None

    def execute(self, sql, args=None):
        self.row = self.rows.get(int(args[0])) if args else None

    def fetchone(self):
        return self.row


class SellableHereTest(unittest.TestCase):
    ROWS = {1: FRAME, 1005: LENS}

    def test_the_cart_refuses_a_lens_on_in_and_accepts_it_on_com(self):
        self.assertFalse(
            catalogue.sellable_here(FakeCursor(self.ROWS), "1005", IN))
        self.assertTrue(
            catalogue.sellable_here(FakeCursor(self.ROWS), "1005", COM))

    def test_the_cart_still_accepts_a_frame_on_both(self):
        for site in (COM, IN):
            self.assertTrue(
                catalogue.sellable_here(FakeCursor(self.ROWS), "1", site), site)

    def test_an_unknown_product_is_left_to_the_callers_own_handling(self):
        # This guard answers "wrong storefront", not "no such product"; the
        # add-to-cart path already has its own answer for a missing row, and
        # returning False here would change that error for every frame.
        self.assertTrue(
            catalogue.sellable_here(FakeCursor(self.ROWS), "999999", IN))


@unittest.skipUnless(AVAILABLE, "no MariaDB available")
class SchemaTest(unittest.TestCase):
    """The DDL, against a real server. Applied the way deploy.py applies it."""

    @classmethod
    def setUpClass(cls):
        cls.db = _connect()
        cur = cls.db.cursor()
        cur.execute("DROP TABLE IF EXISTS contact_lens_variants")
        cur.execute("DROP TABLE IF EXISTS contact_lens_param_rules")
        cur.execute("DROP TABLE IF EXISTS contact_lens_images")
        cur.execute("DROP TABLE IF EXISTS contact_lens_products")
        cur.execute("DROP TABLE IF EXISTS products")
        cur.execute(PRODUCTS_DDL)
        cur.executemany(
            "INSERT INTO products (product_id, product_code, product_name,"
            " product_category, product_slug, product_status, product_quantity)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [(1, "BB44", "Optiwar Baby Frame", "Spectacles Frame",
              "optiwar-baby-frame", "ACTIVE", 5),
             (1005, "CL-ADCL", "Aryan 1-Day Color Contact Lens",
              "Contact Lenses", "aryan-1-day", "ACTIVE", 0)])
        contact_lens.ensure_schema(cur)
        cur.execute("UPDATE products SET product_vertical = 'CONTACT_LENS',"
                    " sell_on_com = 1, sell_on_in = 0 WHERE product_id = 1005")
        cls.db.commit()

    @classmethod
    def tearDownClass(cls):
        cur = cls.db.cursor()
        cur.execute("DROP TABLE IF EXISTS contact_lens_variants")
        cur.execute("DROP TABLE IF EXISTS contact_lens_param_rules")
        cur.execute("DROP TABLE IF EXISTS contact_lens_images")
        cur.execute("DROP TABLE IF EXISTS contact_lens_products")
        cur.execute("DROP TABLE IF EXISTS products")
        cls.db.commit()
        cls.db.close()

    def setUp(self):
        self.cur = self.db.cursor()

    def tearDown(self):
        self.db.rollback()

    def _profile(self, **over):
        row = dict(product_id=1005, brand="Aryan", manufacturer="Aryan Optical",
                   modality="DAILY", lens_type="COLOR", pack_quantity=30,
                   availability="IN_STOCK", color_enabled=1)
        row.update(over)
        cols = ", ".join(row)
        marks = ", ".join(["%s"] * len(row))
        self.cur.execute(
            "REPLACE INTO contact_lens_products (%s) VALUES (%s)"
            % (cols, marks), tuple(row.values()))

    def _variant(self, **over):
        row = dict(product_id=1005, sph=None, cyl=None, axis=None,
                   add_power=None, base_curve=None, diameter=None,
                   color_code="", color_name=None, available=1)
        row.update(over)
        cols = ", ".join(row)
        marks = ", ".join(["%s"] * len(row))
        self.cur.execute(
            "INSERT INTO contact_lens_variants (%s) VALUES (%s)"
            % (cols, marks), tuple(row.values()))

    def test_the_migration_leaves_existing_products_as_eyewear_on_both_sites(self):
        self.cur.execute("SELECT product_vertical, sell_on_com, sell_on_in"
                         " FROM products WHERE product_id = 1")
        self.assertEqual(self.cur.fetchone(),
                         {"product_vertical": "EYEWEAR",
                          "sell_on_com": 1, "sell_on_in": 1})

    def test_a_product_inserted_by_the_old_code_defaults_to_eyewear(self):
        # The columns are NOT NULL, so what makes the migration additive is the
        # DEFAULT: the running release inserts without naming them.
        self.cur.execute(
            "INSERT INTO products (product_id, product_code, product_name,"
            " product_category) VALUES (4242, 'ZZ01', 'New Frame',"
            " 'Spectacles Frame')")
        self.cur.execute("SELECT product_vertical, sell_on_com, sell_on_in"
                         " FROM products WHERE product_id = 4242")
        self.assertEqual(self.cur.fetchone(),
                         {"product_vertical": "EYEWEAR",
                          "sell_on_com": 1, "sell_on_in": 1})

    def test_the_india_predicate_returns_frames_and_no_lens(self):
        self.cur.execute("SELECT product_id FROM products WHERE 1=1"
                         + catalogue.catalogue_site_filter(IN))
        self.assertEqual([r["product_id"] for r in self.cur.fetchall()], [1])

    def test_the_com_predicate_returns_both(self):
        self.cur.execute("SELECT product_id FROM products WHERE 1=1"
                         + catalogue.catalogue_site_filter(COM))
        self.assertEqual(
            sorted(r["product_id"] for r in self.cur.fetchall()), [1, 1005])

    def test_a_lens_keeps_its_own_availability_not_frame_stock(self):
        # product_quantity is 0 and must stay irrelevant: a continuously
        # replenished lens is IN_STOCK or ON_ORDER, never OUT_OF_STOCK.
        self._profile(availability="ON_ORDER", lead_time_days=5)
        self.cur.execute("SELECT availability, lead_time_days"
                         " FROM contact_lens_products WHERE product_id = 1005")
        self.assertEqual(self.cur.fetchone(),
                         {"availability": "ON_ORDER", "lead_time_days": 5})

    def test_plano_is_a_real_power_and_not_the_same_as_no_power(self):
        self._profile()
        self._variant(sph="0.00", color_code="AZURE")
        self._variant(sph=None, color_code="AZURE")
        self.cur.execute(
            "SELECT COUNT(*) c FROM contact_lens_variants"
            " WHERE product_id = 1005 AND sph <=> 0.00")
        self.assertEqual(self.cur.fetchone()["c"], 1)

    def test_a_spherical_row_cannot_be_inserted_twice(self):
        # The point of variant_sig. A plain UNIQUE over the nullable columns
        # admits unlimited copies of (-2.00, NULL, NULL), so re-running the
        # importer would multiply the matrix instead of upserting it.
        import pymysql
        self._profile()
        self._variant(sph="-2.00", base_curve="8.60", diameter="14.20",
                      color_code="AZURE")
        with self.assertRaises(pymysql.err.IntegrityError):
            self._variant(sph="-2.00", base_curve="8.60", diameter="14.20",
                          color_code="AZURE")

    def test_a_toric_row_is_distinct_from_the_spherical_one(self):
        self._profile()
        self._variant(sph="-2.00", color_code="")
        self._variant(sph="-2.00", cyl="-1.25", axis=180, color_code="")
        self.cur.execute("SELECT COUNT(*) c FROM contact_lens_variants"
                         " WHERE product_id = 1005")
        self.assertEqual(self.cur.fetchone()["c"], 2)

    def test_a_combination_is_matched_exactly_and_null_safely(self):
        # What Phase 2 will ask before it lets an eye into the cart: a row
        # exists, or the combination is refused. NULL-safe, so "this lens has no
        # cylinder" matches a request with no cylinder and nothing else.
        self._profile()
        self._variant(sph="-2.00", cyl="-1.25", axis=180, base_curve="8.60",
                      diameter="14.20", color_code="")
        sql = ("SELECT variant_id FROM contact_lens_variants"
               " WHERE product_id = 1005 AND available = 1"
               " AND sph <=> %s AND cyl <=> %s AND axis <=> %s"
               " AND add_power <=> %s")
        self.cur.execute(sql, ("-2.00", "-1.25", 180, None))
        self.assertIsNotNone(self.cur.fetchone())
        for miss in (("-2.00", "-1.25", 175, None),   # wrong axis
                     ("-2.00", None, None, None),     # cylinder dropped
                     ("-2.25", "-1.25", 180, None)):  # nearest power
            self.cur.execute(sql, miss)
            self.assertIsNone(self.cur.fetchone(), miss)

    def test_a_withdrawn_combination_is_kept_but_not_orderable(self):
        self._profile()
        self._variant(sph="-9.50", color_code="")
        self.cur.execute("UPDATE contact_lens_variants SET available = 0"
                         " WHERE product_id = 1005 AND sph <=> -9.50")
        self.cur.execute("SELECT variant_id FROM contact_lens_variants"
                         " WHERE product_id = 1005 AND available = 1"
                         " AND sph <=> -9.50")
        self.assertIsNone(self.cur.fetchone())

    def test_two_colours_can_carry_two_different_images(self):
        # The state being replaced: nine of thirteen colours pointed at the
        # same glitter_gray.jpeg because the dict literal had nowhere else.
        self._profile()
        self.cur.executemany(
            "INSERT INTO contact_lens_images (product_id, color_code,"
            " image_url, image_type, sort_order) VALUES (%s, %s, %s, %s, %s)",
            [(1005, "AZURE", "cl/aryan/azure.jpg", "COLOR", 0),
             (1005, "HAZEL", "cl/aryan/hazel.jpg", "COLOR", 0),
             (1005, None, "cl/aryan/pack.jpg", "PACKAGE", 1)])
        self.cur.execute("SELECT image_url FROM contact_lens_images"
                         " WHERE product_id = 1005 AND color_code = 'HAZEL'")
        self.assertEqual(self.cur.fetchone()["image_url"],
                         "cl/aryan/hazel.jpg")

    def test_a_lens_profile_cannot_exist_without_its_product(self):
        import pymysql
        with self.assertRaises(pymysql.err.IntegrityError):
            self._profile(product_id=999999)

    def test_ensure_schema_is_idempotent(self):
        contact_lens._SCHEMA_READY = False
        try:
            contact_lens.ensure_schema(self.cur)
        finally:
            contact_lens._SCHEMA_READY = True
        self.cur.execute("SELECT sell_on_in FROM products WHERE product_id = 1005")
        self.assertEqual(self.cur.fetchone()["sell_on_in"], 0)


if __name__ == "__main__":
    unittest.main()
