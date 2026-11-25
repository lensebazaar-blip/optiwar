import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import brand_compliance

spec = importlib.util.spec_from_file_location(
    "brand_compliance_apply_test", ROOT / "scripts" / "brand_compliance_apply.py")
apply_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply_script)


class ComplianceDataTest(unittest.TestCase):
    def test_settled_counts_keep_ids_and_spot_checks(self):
        self.assertEqual(len(brand_compliance.HELD), 101)
        self.assertEqual(len(brand_compliance.RENAMED), 146)
        self.assertEqual(len(brand_compliance.FORMER_NAMES), 146)
        self.assertEqual(len(brand_compliance.PHOTO_PENDING), 5)
        self.assertEqual(len(brand_compliance.RELEASED), 29)
        self.assertEqual(brand_compliance.PHOTO_REPLACED, {
            84: "AI20",
            501: "AI64",
            82: "AI21",
            713: "AK09",
            580: "AM58",
            348: "AK02",
            347: "AK03",
        })
        self.assertEqual(brand_compliance.PHOTO_PENDING, {
            767: "AL11",
            770: "AL12",
            26: "AL54",
            29: "AL55",
            584: "AM45",
        })
        self.assertEqual(brand_compliance.PHOTO_REMOVE, {
            335: ["AL74_4.jpg"],
            150: ["AM85_2.jpg", "AM85_4.jpg"],
            154: ["AM84_1.jpg", "AM84_2.jpg", "AM84_4.jpg"],
        })
        self.assertEqual(brand_compliance.STOCK_SET[225]["from"], 1)
        self.assertEqual(brand_compliance.STOCK_SET[225]["to"], 0)
        self.assertEqual(brand_compliance.KEEP_ON_SALE, {1: "BB44", 362: "AK98"})
        self.assertTrue(set(brand_compliance.KEEP_ON_SALE).isdisjoint(
            brand_compliance.HELD))
        self.assertEqual(brand_compliance.HELD[668],
                         ("AB27", "Ray-Ban", "frame_mark"))
        self.assertEqual(brand_compliance.RELEASED[332], "AL69")
        self.assertNotIn(332, brand_compliance.HELD)
        self.assertEqual(brand_compliance.RENAMED[332][1],
                         "OPTIWAR Round Metal")
        self.assertEqual(brand_compliance.RENAMED[671][1],
                         "OPTIWAR Browline")
        self.assertEqual(brand_compliance.RENAMED[33],
                         ("AM09", "OPTIWAR Geometric"))
        self.assertEqual(brand_compliance.RENAMED[150],
                         ("AM85", "OPTIWAR Rimless"))
        self.assertEqual(brand_compliance.RENAMED[860],
                         ("AG74", "OPTIWAR Round"))
        self.assertEqual(brand_compliance.RENAMED[46],
                         ("AE19", "OPTIWAR Round"))
        self.assertEqual(brand_compliance.RENAMED[577],
                         ("BQ79", "OPTIWAR Half-Rim"))
        self.assertEqual(brand_compliance.RENAMED[158],
                         ("AG23", "OPTIWAR Rimless"))
        self.assertEqual(brand_compliance.RENAMED[571],
                         ("BQ80", "OPTIWAR Half-Rim"))
        self.assertEqual(brand_compliance.RENAMED[574],
                         ("BQ78", "OPTIWAR Half-Rim"))
        self.assertEqual(brand_compliance.RENAMED[575],
                         ("BQ81", "OPTIWAR Half-Rim"))
        self.assertEqual(brand_compliance.FORMER_NAMES[158], "ODYSEY")
        for product_id in (571, 574, 575):
            self.assertEqual(
                brand_compliance.FORMER_NAMES[product_id], "FIRST CLASS")
        self.assertEqual(brand_compliance.HELD[93],
                         ("AH69", "Tommy Hilfiger (lookalike flag)", "frame_mark"))
        self.assertEqual(brand_compliance.HELD[961],
                         ("AB87", "Royal Navy", "frame_mark"))
        self.assertEqual(brand_compliance.HELD[482],
                         ("AA05", "Oppo/Vivo (oppvivo)", "frame_mark"))
        self.assertEqual(brand_compliance.RENAMED[150],
                         ("AM85", "OPTIWAR Rimless"))
        self.assertEqual(brand_compliance.RENAMED[154],
                         ("AM84", "OPTIWAR Rimless"))
        self.assertEqual(brand_compliance.FORMER_NAMES[150],
                         ("DASAULTS", "OPTIWAR Frame"))
        self.assertEqual(brand_compliance.FORMER_NAMES[154],
                         ("DASAULTS", "OPTIWAR Frame"))
        self.assertEqual(brand_compliance.FORMER_NAMES[46], "RISE ABOVE")

    def test_bb_frames_are_released_and_unblocked(self):
        released = {
            261: "BB22",
            146: "BB26",
            145: "BB27",
            203: "BB31",
            204: "BB32",
            366: "BB33",
            411: "BB74",
            302: "BB85",
            865: "BB94",
        }
        for product_id, sku in released.items():
            with self.subTest(product_id=product_id):
                self.assertNotIn(product_id, brand_compliance.HELD)
                self.assertEqual(brand_compliance.RELEASED.get(product_id), sku)
                self.assertNotIn(product_id, brand_compliance.PHOTO_PENDING)
                self.assertNotIn(product_id, brand_compliance.blocked_ids())
                self.assertNotIn(product_id, brand_compliance.held_ids())
                self.assertFalse(
                    brand_compliance.is_held({"product_id": product_id}))

    def test_al69_and_al09_are_released_without_photo_pending(self):
        for product_id, sku in ((332, "AL69"), (768, "AL09")):
            with self.subTest(product_id=product_id):
                self.assertEqual(brand_compliance.RELEASED[product_id], sku)
                self.assertNotIn(product_id, brand_compliance.PHOTO_PENDING)

    def test_photo_batches_2_to_4_are_not_held_or_photo_pending(self):
        released = {
            675: "AD99",
            808: "AL37",
            814: "AL38",
            813: "AL40",
            336: "AL70",
            330: "AL71",
            333: "AL73",
            334: "AL75",
            331: "AL76",
            672: "AD62",
            860: "AG74",
            587: "AM44",
        }
        for product_id, sku in released.items():
            with self.subTest(product_id=product_id):
                self.assertNotIn(product_id, brand_compliance.HELD)
                self.assertNotIn(product_id, brand_compliance.PHOTO_PENDING)
                self.assertEqual(brand_compliance.RENAMED[product_id][0], sku)

    def test_new_names_do_not_repeat_the_sku(self):
        # every page prints product_name then product_code
        for product_id, (sku, name) in brand_compliance.RENAMED.items():
            self.assertTrue(name.startswith("OPTIWAR "), name)
            self.assertNotIn(sku, name)

    def test_models_imports_every_compliance_name_it_uses(self):
        # held_ids was used by /sitemap.xml without being imported: a NameError
        # only once the static sitemap file exists, which no test had.
        import ast
        source = (ROOT / "models.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imported.update((a.asname or a.name).split(".")[0] for a in node.names)
        public = {n for n in dir(brand_compliance) if not n.startswith("_")}
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        self.assertEqual(sorted((used & public) - imported - {"re", "unicodedata"}), [])

    def test_every_renamed_frame_has_its_former_name_and_text_is_cleaned(self):
        self.assertEqual(set(brand_compliance.FORMER_NAMES), set(brand_compliance.RENAMED))
        self.assertEqual(
            brand_compliance.scrub_text(
                "STAR WARS BO76 - Black frame with Red Black temple arms.", 417),
            "OPTIWAR Round BO76 - Black frame with Red Black temple arms.")
        # whatever the case, and only for that product
        self.assertEqual(brand_compliance.scrub_text("Star Wars frame", 417),
                         "OPTIWAR Round frame")
        self.assertEqual(brand_compliance.scrub_text("STAR WARS BO76", 1), "STAR WARS BO76")
        self.assertIsNone(brand_compliance.scrub_text(None, 417))
        for product_id, former in brand_compliance.FORMER_NAMES.items():
            formers = (former,) if isinstance(former, str) else former
            for name in formers:
                cleaned = brand_compliance.scrub_text(
                    "%s X - frame." % name, product_id)
                self.assertNotIn(name.lower(), cleaned.lower(), name)

    def test_reshape_and_maker_renames_scrub_names_and_seo(self):
        reshape = _row(
            33, "AM09", product_name="OPTIWAR Frame",
            product_slug="optiwar-frame",
            product_details="OPTIWAR Frame AM09 - AJ 6 details.",
            description_seo="OPTIWAR Frame, formerly AJ 6.")
        changes = apply_script._changes(reshape)
        self.assertEqual(changes["product_name"], "OPTIWAR Geometric")
        self.assertEqual(changes["product_slug"], "optiwar-geometric")
        self.assertEqual(
            changes["product_details"],
            "OPTIWAR Geometric AM09 - OPTIWAR Geometric details.")
        self.assertEqual(
            changes["description_seo"],
            "OPTIWAR Geometric, formerly OPTIWAR Geometric.")
        self.assertNotIn("sell_on_in", changes)
        self.assertNotIn("sell_on_com", changes)

        maker = _row(
            46, "AE19", product_name="RISE ABOVE",
            product_slug="rise-above",
            product_details="RISE ABOVE AE19 frame.",
            description_seo="RISE ABOVE frame.")
        maker_changes = apply_script._changes(maker)
        self.assertEqual(maker_changes["product_name"], "OPTIWAR Round")
        self.assertEqual(maker_changes["product_slug"], "optiwar-round")
        self.assertEqual(
            maker_changes["product_details"], "OPTIWAR Round AE19 frame.")
        self.assertEqual(
            maker_changes["description_seo"], "OPTIWAR Round frame.")
        self.assertNotIn("sell_on_in", maker_changes)
        self.assertNotIn("sell_on_com", maker_changes)
        self.assertEqual(brand_compliance.display_name(860), "OPTIWAR Round")
        self.assertEqual(
            brand_compliance.scrub_text("LOUIS STYLE N-5075 AG74", 860),
            "OPTIWAR Round AG74")
        self.assertEqual(
            brand_compliance.scrub_text("OPTIWAR Frame AG74", 860),
            "OPTIWAR Round AG74")

    def test_followup_maker_renames_scrub_text_and_preserve_sell_flags(self):
        cases = (
            (158, "AG23", "ODYSEY", "OPTIWAR Rimless"),
            (571, "BQ80", "FIRST CLASS", "OPTIWAR Half-Rim"),
            (574, "BQ78", "FIRST CLASS", "OPTIWAR Half-Rim"),
            (575, "BQ81", "FIRST CLASS", "OPTIWAR Half-Rim"),
        )
        for product_id, sku, former, new_name in cases:
            row = _row(
                product_id, sku, product_name=former,
                product_slug=brand_compliance.slugify(former),
                product_details="%s %s frame." % (former, sku),
                description_seo="%s frame." % former,
                sell_on_in=1, sell_on_com=1)
            changes = apply_script._changes(row)
            with self.subTest(product_id=product_id):
                self.assertEqual(changes["product_name"], new_name)
                self.assertEqual(changes["product_slug"],
                                 brand_compliance.slugify(new_name))
                self.assertEqual(changes["product_details"],
                                 "%s %s frame." % (new_name, sku))
                self.assertEqual(changes["description_seo"],
                                 "%s frame." % new_name)
                self.assertNotIn("sell_on_in", changes)
                self.assertNotIn("sell_on_com", changes)

    def test_renamed_page_reports_its_new_address_and_description(self):
        source = (ROOT / "models.py").read_text(encoding="utf-8")
        self.assertIn('product["product_slug"] = expected_slug', source)
        self.assertIn('product["product_details"] = scrub_text('
                      'product.get("product_details"), product_id)', source)

    def test_held_helpers_and_storefront_predicates(self):
        import catalogue
        predicate = catalogue.catalogue_site_filter()
        self.assertIn("NOT IN", predicate)
        self.assertIn("668", predicate)
        self.assertNotIn("675", predicate)
        self.assertIn("767", predicate)
        self.assertNotIn("696", predicate)
        self.assertTrue(brand_compliance.is_held(668))
        self.assertTrue(brand_compliance.is_held({"product_id": 668}))
        self.assertFalse(brand_compliance.is_held(675))
        self.assertFalse(brand_compliance.is_held({"product_id": 675}))
        self.assertTrue(brand_compliance.is_held(767))
        self.assertFalse(brand_compliance.is_held(696))
        self.assertFalse(brand_compliance.is_held(1))
        self.assertFalse(brand_compliance.is_held(362))
        self.assertEqual(brand_compliance.held_ids(),
                         brand_compliance.blocked_ids())
        for site in (catalogue.SITE_COM, catalogue.SITE_IN):
            self.assertFalse(catalogue.is_product_allowed(668, site))
            self.assertTrue(catalogue.is_product_allowed(
                {"product_id": 1, "product_vertical": "EYEWEAR"}, site))
            self.assertTrue(catalogue.is_product_allowed(
                {"product_id": 675, "product_vertical": "EYEWEAR",
                 "sell_on_com": 1, "sell_on_in": 1}, site))
            self.assertFalse(catalogue.is_product_allowed(
                {"product_id": 767, "product_vertical": "EYEWEAR",
                 "sell_on_com": 1, "sell_on_in": 1}, site))
            self.assertTrue(catalogue.is_product_allowed(
                {"product_id": 696, "product_vertical": "EYEWEAR",
                 "sell_on_com": 1, "sell_on_in": 1}, site))

    def test_matcher_and_public_style_sources_exclude_old_labels(self):
        from tests.test_frame_offers import offers
        self.assertIn("668", offers.match_sql("optiwar.com"))
        self.assertNotIn("675", offers.match_sql("optiwar.com"))
        self.assertIn("767", offers.match_sql("optiwar.com"))
        self.assertNotIn("696", offers.match_sql("optiwar.com"))
        self.assertIn("NOT IN", offers.match_sql("optiwar.com"))
        self.assertEqual(
            brand_compliance.STYLE_LABELS["product_category_clubmaster"],
            "Browline")
        self.assertEqual(
            brand_compliance.STYLE_LABELS["product_category_wayfarer"],
            "Classic Square")
        for path in (ROOT / "templates").rglob("*"):
            if not path.is_file() or "templates/ai_glasses/" in str(path):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            self.assertFalse(any(word in text for word in brand_compliance.BANNED_STYLE_WORDS),
                             str(path))
        for path in (ROOT / "static" / "ai").glob("*.json"):
            text = path.read_text(encoding="utf-8").lower()
            self.assertFalse(any(word in text for word in brand_compliance.BANNED_STYLE_WORDS),
                             str(path))


class _ProductCursor:
    def __init__(self, product):
        self.product = product

    def execute(self, sql, params=()):
        pass

    def fetchone(self):
        return self.product

    def close(self):
        pass


class _ProductDB:
    def __init__(self, product):
        self.cursor_obj = _ProductCursor(product)

    def cursor(self):
        return self.cursor_obj


class ProductRouteTest(unittest.TestCase):
    @staticmethod
    def _isolated_import(name):
        return name in ("flask_mail", "optiwar", "flaskr") or name.startswith(
            ("flask_mail.", "optiwar.", "flaskr."))

    @classmethod
    def setUpClass(cls):
        cls._saved_modules = {}
        for name in list(sys.modules):
            if cls._isolated_import(name):
                cls._saved_modules[name] = sys.modules.pop(name)
        cls.addClassCleanup(cls._restore_imports)
        sys.path.insert(0, str(ROOT.parent))
        import pymysql
        pymysql.install_as_MySQLdb()
        try:
            import optiwar
            sys.modules.setdefault("flaskr", optiwar)
            from optiwar import models
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(
                "Optiwar route imports unavailable dependency: %s" % exc.name)
        cls.models = models
        cls.app = Flask(__name__)
        cls.app.secret_key = "test"
        cls.app.register_blueprint(models.bp)

    @classmethod
    def _restore_imports(cls):
        saved_modules = getattr(cls, "_saved_modules", None)
        if saved_modules is None:
            return
        for name in list(sys.modules):
            if cls._isolated_import(name):
                del sys.modules[name]
        sys.modules.update(saved_modules)
        cls._saved_modules = None

    @classmethod
    def tearDownClass(cls):
        cls._restore_imports()

    def test_category_aliases_redirect(self):
        client = self.app.test_client()
        self.assertEqual(client.get("/categories/clubmaster").status_code, 301)
        self.assertEqual(client.get("/categories/clubmaster").location,
                         "/categories/browline")
        self.assertEqual(client.get("/categories/wayfarer").status_code, 301)
        self.assertEqual(client.get("/categories/wayfarer").location,
                         "/categories/square")

    def test_legacy_contact_lens_routes_redirect_to_listing(self):
        client = self.app.test_client()
        for path in ("/contact_lenses/x", "/contact_lenses/x/y"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 301)
                self.assertEqual(response.location, "/contact-lenses")

    def test_sitemap_serves_the_static_file_without_held_frames(self):
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<urlset>\n'
            '  <url><loc>https://optiwar.com/</loc></url>\n'
            '  <url><loc>https://optiwar.com/categories/spectacles-frame/boss?pid=665</loc></url>\n'
            '  <url><loc>https://optiwar.com/categories/spectacles-frame/louis-style?pid=671</loc></url>\n'
            '  <url><loc>https://optiwar.com/categories/spectacles-frame/ad99?pid=675</loc></url>\n'
            '  <url><loc>https://optiwar.com/categories/spectacles-frame/al11?pid=767</loc></url>\n'
            '  <url><loc>https://optiwar.com/categories/spectacles-frame/rb-ab85?pid=696</loc></url>\n'
            '  <url><loc>https://optiwar.com/categories/spectacles-frame/optiwar-baby-frame?pid=1</loc></url>\n'
            '</urlset>')
        new_slug = brand_compliance.slugify(brand_compliance.RENAMED[671][1])
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "static", "seo"))
            with open(os.path.join(root, "static", "seo", "sitemap.xml"), "w") as fh:
                fh.write(xml)
            old_root = self.app.root_path
            self.app.root_path = root
            try:
                for host in ("optiwar.com", "optiwar.in"):
                    with self.subTest(host=host), \
                            patch.object(self.models, "get_db", return_value=_ProductDB(None)), \
                            patch.object(self.models, "_live_lens_rows", return_value=[]):
                        response = self.app.test_client().get(
                            "/sitemap.xml", base_url="https://%s" % host)
                        body = response.get_data(as_text=True)
                        self.assertEqual(response.status_code, 200)
                        self.assertNotIn("pid=665", body)
                        self.assertNotIn("pid=767", body)
                        self.assertIn("pid=675", body)
                        self.assertIn("/%s?pid=671" % new_slug, body)
                        self.assertIn(
                            "/%s?pid=696" % brand_compliance.slugify(
                                brand_compliance.RENAMED[696][1]), body)
                        self.assertIn("pid=1<", body)
                        self.assertIn("https://%s/" % host, body)
            finally:
                self.app.root_path = old_root

    def test_held_product_page_moves_to_its_nearest_category(self):
        # no page for a held frame, but its old address keeps its visitors and
        # its links: a permanent redirect to frames of the same shape
        cases = (
            ({"product_category_clubmaster": 1}, "/categories/browline"),
            ({"product_category_wayfarer": 1}, "/categories/square"),
            ({"product_category_round": 1}, "/categories/round"),
            ({}, "/eyeglasses/all-spectacle-frames.html"),
        )
        for flags, target in cases:
            with self.subTest(target=target):
                product = _row(668, "AB27", product_category="Spectacles Frame",
                               product_slug="ray-ban-ab27", product_status="ACTIVE",
                               product_vertical="EYEWEAR", **flags)
                # already switched off in the database: still a redirect, not a 404
                product.update(sell_on_in=0, sell_on_com=0)
                with patch.object(self.models, "get_db", return_value=_ProductDB(product)):
                    response = self.app.test_client().get(
                        "/categories/spectacles-frame/ray-ban-ab27?pid=668")
                self.assertEqual(response.status_code, 301)
                self.assertEqual(response.location, target)

    def test_photo_pending_product_is_redirected_but_ab85_is_not_blocked(self):
        pending = _row(767, "AL11", product_category="Spectacles Frame",
                       product_slug="al11", product_status="ACTIVE",
                       product_vertical="EYEWEAR")
        with patch.object(self.models, "get_db", return_value=_ProductDB(pending)):
            response = self.app.test_client().get(
                "/categories/spectacles-frame/al11?pid=767")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response.location, "/eyeglasses/all-spectacle-frames.html")

        self.assertNotIn(696, brand_compliance.blocked_ids())
        self.assertFalse(brand_compliance.is_held(675))
        self.assertTrue(brand_compliance.is_held(767))
        self.assertFalse(brand_compliance.is_held(696))

    def test_a_product_that_does_not_exist_is_still_not_found(self):
        with patch.object(self.models, "get_db", return_value=_ProductDB(None)):
            response = self.app.test_client().get(
                "/categories/spectacles-frame/nothing?pid=999999")
        self.assertEqual(response.status_code, 404)

    def test_renamed_product_old_slug_redirects_per_id(self):
        product = _row(671, "AD61", product_category="Spectacles Frame",
                       product_slug="old-ad61", product_status="ACTIVE",
                       product_vertical="EYEWEAR")
        with patch.object(self.models, "get_db", return_value=_ProductDB(product)):
            response = self.app.test_client().get(
                "/categories/spectacles-frame/old-ad61?pid=671")
        self.assertEqual(response.status_code, 301)
        self.assertIn(brand_compliance.slugify(
            brand_compliance.RENAMED[671][1]), response.location)

    def test_round_two_old_slugs_redirect_for_am03_and_ab85(self):
        cases = (
            (764, "AM03", "aj-6013-am03", 1, 1, 1),
            (696, "AB85", "rb-ab85", 1, 1, 1),
            (784, "AH08", "louis-style-k-8003a", 0, 0, 0),
        )
        for (product_id, sku, old_slug, sell_on_com, sell_on_in,
             quantity) in cases:
            product = _row(
                product_id, sku, product_category="Spectacles Frame",
                product_slug=old_slug, product_status="ACTIVE",
                product_vertical="EYEWEAR",
                sell_on_com=sell_on_com, sell_on_in=sell_on_in,
                product_quantity=quantity)
            with self.subTest(product_id=product_id), \
                    patch.object(self.models, "get_db",
                                 return_value=_ProductDB(product)):
                response = self.app.test_client().get(
                    "/categories/spectacles-frame/%s?pid=%s"
                    % (old_slug, product_id))
            expected = brand_compliance.slugify(
                brand_compliance.RENAMED[product_id][1])
            self.assertEqual(response.status_code, 301)
            self.assertIn("/categories/spectacles-frame/%s?pid=%s" %
                          (expected, product_id), response.location)

    def test_shape_and_maker_old_slugs_redirect_by_product_id(self):
        cases = (
            (33, "AM09", "optiwar-frame", 1, 1, 1),
            (33, "AM09", "aj-6", 1, 1, 1),
            (46, "AE19", "rise-above", 0, 0, 0),
            (571, "BQ80", "first-class", 1, 1, 2),
        )
        for (product_id, sku, old_slug, sell_on_com, sell_on_in,
             quantity) in cases:
            product = _row(
                product_id, sku, product_category="Spectacles Frame",
                product_slug=old_slug, product_status="ACTIVE",
                product_vertical="EYEWEAR", sell_on_com=sell_on_com,
                sell_on_in=sell_on_in, show_in_listings=int(bool(quantity)),
                product_quantity=quantity)
            with self.subTest(product_id=product_id, old_slug=old_slug), \
                    patch.object(self.models, "get_db",
                                 return_value=_ProductDB(product)):
                response = self.app.test_client().get(
                    "/categories/spectacles-frame/%s?pid=%s"
                    % (old_slug, product_id))
            expected = brand_compliance.slugify(
                brand_compliance.RENAMED[product_id][1])
            self.assertEqual(response.status_code, 301)
            self.assertIn("/categories/spectacles-frame/%s?pid=%s" %
                          (expected, product_id), response.location)


class _Cursor:
    def __init__(self):
        self.queries = []
        self.fetchone_result = None

    def execute(self, sql, params=()):
        self.queries.append((sql, params))

    def fetchone(self):
        return self.fetchone_result


class _DB:
    def __init__(self):
        self.cursor_obj = _Cursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def _row(product_id, code, **updates):
    row = {
        "product_id": product_id, "product_code": code,
        "product_name": "Original " + code, "product_slug": "original-" + code.lower(),
        "sell_on_in": 1, "sell_on_com": 1, "show_in_listings": 1,
        "product_category_browline": 0, "product_category_clubmaster": 0,
        "product_category_square": 0, "product_category_wayfarer": 0,
        "product_image": "", "product_details": "",
    }
    row.update(updates)
    return row


def _already_named_photo_remove_row():
    name = brand_compliance.RENAMED[335][1]
    return _row(
        335, "AL74", product_name=name,
        product_slug=brand_compliance.slugify(name),
        product_image="catalog/opticalbazaar/AL74/AL74_4.jpg")


class ApplyScriptTest(unittest.TestCase):
    def setUp(self):
        self.photo_remove_patcher = patch.object(
            apply_script.brand_compliance, "PHOTO_REMOVE",
            {335: ["AL74_4.jpg"]})
        self.photo_remove_patcher.start()
        self.addCleanup(self.photo_remove_patcher.stop)

    def test_approved_r2_lists_only_sorted_master_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            r2 = Path(tmp) / "catalog" / "r2" / "AI20"
            for relative in (
                    "AI20_r2_02.jpg",
                    "AI20_r2_01.jpg",
                    "AI20_notes.txt",
                    "derivatives/AI20_r2_01-200.jpg"):
                path = r2 / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"image")

            self.assertEqual(apply_script._approved_r2("AI20", tmp), [
                "catalog/r2/AI20/AI20_r2_01.jpg",
                "catalog/r2/AI20/AI20_r2_02.jpg",
            ])

    def test_photo_replacement_quarantines_families_and_restores_old_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_root = Path(tmp) / "static"
            quarantine = Path(tmp) / "quarantine"
            restore_dir = Path(tmp) / "restore"
            round1_restore = Path(tmp) / "round1.json"
            old_paths = [
                Path("catalog/opticalbazaar/AI20/AI20_%d.jpg" % index)
                for index in range(1, 5)
            ]
            old_original = ",".join("./" + path.as_posix()
                                    for path in old_paths)
            for relative in old_paths:
                path = image_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(relative.name.encode())
                derivative = (
                    image_root / relative.parent / "derivatives"
                    / (relative.stem + "-200.webp"))
                derivative.parent.mkdir(parents=True, exist_ok=True)
                derivative.write_bytes(b"old derivative")

            approved = [
                "catalog/r2/AI20/AI20_r2_01.jpg",
                "catalog/r2/AI20/AI20_r2_02.jpg",
            ]
            for relative in approved:
                path = image_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"approved retouch")
            name = brand_compliance.RENAMED[84][1]
            row = _row(
                84, "AI20", product_image=old_original,
                product_name=name, product_slug=brand_compliance.slugify(name))

            dry_output = io.StringIO()
            dry_db = _DB()
            with patch.object(apply_script, "_load_rows", return_value=[row]), \
                    patch.object(apply_script.brand_compliance,
                                 "PHOTO_REMOVE", {}), \
                    redirect_stdout(dry_output):
                apply_script.run(
                    dry_db, types.SimpleNamespace(
                        apply=False, quarantine=False),
                    str(image_root), str(quarantine), str(restore_dir),
                    str(round1_restore))
            self.assertIn("REPLACE PHOTOS AI20 old=4 new=2",
                          dry_output.getvalue())
            self.assertIn("photos to quarantine=8", dry_output.getvalue())
            self.assertIn("photos replaced=1", dry_output.getvalue())
            self.assertEqual(dry_db.cursor_obj.queries, [])
            self.assertTrue(all((image_root / path).is_file()
                                for path in old_paths))

            db = _DB()
            with patch.object(apply_script, "_load_rows", return_value=[row]), \
                    patch.object(apply_script.brand_compliance,
                                 "PHOTO_REMOVE", {}):
                apply_script.run(
                    db, types.SimpleNamespace(
                        apply=True, quarantine=True),
                    str(image_root), str(quarantine), str(restore_dir),
                    str(round1_restore))

            new_image_value = ",".join(approved)
            self.assertTrue(any(
                "product_image=%s" in sql and new_image_value in params
                for sql, params in db.cursor_obj.queries))
            self.assertTrue(all(not (image_root / path).exists()
                                for path in old_paths))
            client = Flask(
                "photo_replacement_static_test",
                static_folder=str(image_root),
                static_url_path="/static").test_client()
            for relative in old_paths:
                response = client.get("/static/" + relative.as_posix())
                try:
                    self.assertEqual(response.status_code, 404)
                finally:
                    response.close()

            restore_file = next(
                restore_dir.glob("brand_compliance_restore_*.json"))
            record = json.loads(restore_file.read_text())
            self.assertEqual(record["updates"]["84"]["product_image"],
                             old_original)
            self.assertEqual(len(record["image_moves"]), 8)
            restore_db = _DB()
            apply_script.restore(
                restore_db, str(restore_file), str(image_root), str(quarantine))
            self.assertTrue(all((image_root / path).is_file()
                                for path in old_paths))
            self.assertTrue(any(
                "product_image=%s" in sql and old_original in params
                for sql, params in restore_db.cursor_obj.queries))

    def test_photo_replacement_skips_frames_with_fewer_than_two_approved_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_root = Path(tmp) / "static"
            quarantine = Path(tmp) / "quarantine"
            restore_dir = Path(tmp) / "restore"
            round1_restore = Path(tmp) / "round1.json"
            approved = image_root / "catalog" / "r2" / "AI20" / "AI20_r2_01.jpg"
            approved.parent.mkdir(parents=True)
            approved.write_bytes(b"single approved photo")
            name = brand_compliance.RENAMED[84][1]
            row = _row(
                84, "AI20", product_image="catalog/old/AI20_1.jpg",
                product_name=name, product_slug=brand_compliance.slugify(name))
            db = _DB()
            output = io.StringIO()
            with patch.object(apply_script, "_load_rows", return_value=[row]), \
                    patch.object(apply_script.brand_compliance,
                                 "PHOTO_REMOVE", {}), \
                    redirect_stdout(output):
                apply_script.run(
                    db, types.SimpleNamespace(
                        apply=False, quarantine=False),
                    str(image_root), str(quarantine), str(restore_dir),
                    str(round1_restore))

            self.assertIn(
                "MISSING approved R2 photos for AI20 (found 1, need at least 2)",
                output.getvalue())
            self.assertIn("photos replaced=0", output.getvalue())
            self.assertNotIn("product_image=", output.getvalue())
            self.assertEqual(db.cursor_obj.queries, [])

    def test_released_r2_set_replaces_old_photo_count_without_copying(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_root = Path(tmp) / "static"
            approved = [
                "catalog/r2/AB85/AB85_r2_01.jpg",
                "catalog/r2/AB85/AB85_r2_02.jpg",
                "catalog/r2/AB85/AB85_r2_03.jpg",
            ]
            for relative in approved:
                path = image_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"approved image")
            old_paths = [
                "catalog/opticalbazaar/AB85/AB85_%d.jpg" % index
                for index in range(1, 5)
            ]
            row = _row(696, "AB85", product_image=",".join(old_paths))

            plan = apply_script._republish_plan(
                row, str(image_root), str(Path(tmp) / "quarantine"), {})

            self.assertTrue(plan["ready"])
            self.assertEqual(plan["image_value"], ",".join(approved))
            self.assertEqual(plan["copies"], [])
            self.assertEqual(plan["old_public"], old_paths)
            self.assertEqual(plan["missing"], [])

    def test_existing_release_r2_values_remain_unchanged_for_ten_frames(self):
        released = {
            696: "AB85",
            261: "BB22",
            146: "BB26",
            145: "BB27",
            203: "BB31",
            204: "BB32",
            366: "BB33",
            411: "BB74",
            302: "BB85",
            865: "BB94",
        }
        with tempfile.TemporaryDirectory() as tmp:
            image_root = Path(tmp) / "static"
            rows = []
            for product_id, sku in released.items():
                paths = [
                    "catalog/r2/%s/%s_r2_%02d.jpg" % (sku, sku, index)
                    for index in (1, 2)
                ]
                for relative in paths:
                    path = image_root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"approved image")
                renamed = brand_compliance.RENAMED.get(product_id)
                name = renamed[1] if renamed else "Original " + sku
                rows.append(_row(
                    product_id, sku, product_image=",".join(paths),
                    product_name=name,
                    product_slug=brand_compliance.slugify(name)))

            db = _DB()
            output = io.StringIO()
            with patch.object(apply_script, "_load_rows", return_value=rows), \
                    patch.object(apply_script.brand_compliance,
                                 "PHOTO_REMOVE", {}), \
                    redirect_stdout(output):
                apply_script.run(
                    db, types.SimpleNamespace(
                        apply=False, quarantine=False),
                    str(image_root), str(Path(tmp) / "quarantine"),
                    str(Path(tmp) / "restore"),
                    str(Path(tmp) / "round1.json"))

            self.assertIn("photos to republish=0", output.getvalue())
            self.assertIn("photos replaced=0", output.getvalue())
            self.assertNotIn("product_image=", output.getvalue())
            self.assertEqual(db.cursor_obj.queries, [])

    def test_al69_and_al09_republish_approved_r2_sets_over_current_images(self):
        fixtures = {
            332: {
                "sku": "AL69",
                "product_image": ",".join(
                    "./catalog/opticalbazaar/AL69/AL69_%d.jpg" % index
                    for index in range(1, 5)),
                "count": 4,
            },
            768: {
                "sku": "AL09",
                "product_image": ",".join(
                    "./catalog/opticalbazaar/AL09/AL09_%d.jpg" % index
                    for index in range(1, 4)),
                "count": 3,
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            image_root = Path(tmp) / "static"
            for product_id, fixture in fixtures.items():
                sku = fixture["sku"]
                approved = [
                    "catalog/r2/%s/%s_r2_%02d.jpg" % (sku, sku, index)
                    for index in range(1, fixture["count"] + 1)
                ]
                for relative in approved:
                    path = image_root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"approved image")
                name = brand_compliance.RENAMED[product_id][1]
                row = _row(
                    product_id, sku,
                    product_image=fixture["product_image"],
                    product_name=name,
                    product_slug=brand_compliance.slugify(name))

                plan = apply_script._republish_plan(
                    row, str(image_root), str(Path(tmp) / "quarantine"), {})

                with self.subTest(product_id=product_id):
                    self.assertTrue(plan["ready"])
                    self.assertEqual(plan["image_value"], ",".join(approved))
                    self.assertEqual(
                        plan["old_public"], apply_script._image_paths(row))
                    self.assertEqual(plan["copies"], [])
                    self.assertEqual(plan["missing"], [])

    def test_ab87_quarantines_both_product_image_families(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_root = Path(tmp) / "static"
            quarantine_dir = Path(tmp) / "quarantine" / "timestamp"
            first = Path("catalog/opticalbazaar/AB14/AB87/AB87_1.jpg")
            second = Path("catalog/opticalbazaar/AB87/AB87_2.jpg")
            relatives = [
                first,
                Path("catalog/opticalbazaar/AB14/AB87/derivatives/"
                     "AB87_1-200.webp"),
                second,
                Path("catalog/opticalbazaar/AB87/derivatives/"
                     "AB87_2-200.webp"),
            ]
            for relative in relatives:
                path = image_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(relative.name.encode())
            row = _row(
                961, "AB87",
                product_image="./%s,./%s" % (
                    first.as_posix(), second.as_posix()))

            blocked, _ = apply_script._quarantine_plan(
                [row], [], str(image_root), str(quarantine_dir),
                str(quarantine_dir.parent), {"image_moves": []})

            planned, already, missing = blocked
            self.assertEqual(
                {Path(item["source"]).relative_to(image_root).as_posix()
                 for item in planned},
                {path.as_posix() for path in relatives})
            self.assertEqual(already, [])
            self.assertEqual(missing, [])

    def test_added_photo_remove_files_are_selected(self):
        photo_remove = {
            150: ["AM85_2.jpg", "AM85_4.jpg"],
            154: ["AM84_1.jpg", "AM84_2.jpg", "AM84_4.jpg"],
        }
        with patch.object(apply_script.brand_compliance, "PHOTO_REMOVE",
                          photo_remove):
            for product_id, sku, filenames in (
                    (150, "AM85", photo_remove[150]),
                    (154, "AM84", photo_remove[154])):
                keep = "catalog/opticalbazaar/%s/%s_keep.jpg" % (
                    sku, sku)
                removals = [
                    "catalog/opticalbazaar/%s/%s" % (sku, filename)
                    for filename in filenames]
                row = _row(
                    product_id, sku,
                    product_image=",".join(
                        ["./" + keep] + ["./" + path for path in removals]))

                plan = apply_script._photo_remove_plan(row)

                self.assertEqual(set(plan["paths"]), set(removals))
                self.assertEqual(plan["image_value"], "./" + keep)

    def test_photo_remove_drops_found_image_when_another_is_missing(self):
        original = [
            "./catalog/opticalbazaar/AM85/AM85_1.jpg",
            "./catalog/opticalbazaar/AM85/AM85_3.jpg",
            "./catalog/opticalbazaar/AM85/AM85_4.jpg",
        ]
        row = _row(150, "AM85", product_image=",".join(original))
        with patch.object(apply_script.brand_compliance, "PHOTO_REMOVE",
                          {150: ["AM85_2.jpg", "AM85_4.jpg"]}):
            plan = apply_script._photo_remove_plan(row)

        self.assertEqual(plan["image_value"], ",".join(original[:2]))
        self.assertEqual(
            plan["paths"], ["catalog/opticalbazaar/AM85/AM85_4.jpg"])
        self.assertEqual(plan["missing"], ["AM85_2.jpg"])

    def test_round_one_seo_descriptions_are_scrubbed(self):
        row = _row(
            671, "AD61", description_seo="LOUIS STYLE AD61 acetate frame.")
        self.assertEqual(
            apply_script._changes(row)["description_seo"],
            "OPTIWAR Browline AD61 acetate frame.")

    def test_main_passes_custom_image_root_to_dry_run_and_restore(self):
        image_root = "/live/flaskr/static"
        db = _DB()
        with patch.object(apply_script, "connect", return_value=db), \
                patch.object(apply_script, "run", return_value=0) as run:
            self.assertEqual(apply_script.main(["--image-root", image_root]), 0)
        self.assertEqual(run.call_args.kwargs["image_root"], image_root)

        with patch.object(apply_script, "connect", return_value=db), \
                patch.object(apply_script, "restore", return_value=0) as restore:
            self.assertEqual(apply_script.main([
                "--restore", "/root/record.json", "--image-root", image_root]), 0)
        self.assertEqual(restore.call_args.kwargs["image_root"], image_root)

    def test_dry_run_apply_idempotency_restore_and_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_root = Path(tmp) / "static"
            source = image_root / "catalog" / "ab27.jpg"
            sticker_source = image_root / "catalog" / "al11.jpg"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"frame")
            sticker_source.write_bytes(b"lens sticker")
            static_client = Flask(
                "brand_compliance_static_test",
                static_folder=str(image_root),
                static_url_path="/static").test_client()

            def static_status(path):
                response = static_client.get(path)
                try:
                    return response.status_code
                finally:
                    response.close()

            self.assertEqual(static_status("/static/catalog/al11.jpg"), 200)
            quarantine = Path(tmp) / "quarantine"
            restore_dir = Path(tmp) / "restore"
            rows = [
                _row(668, "AB27", product_image="./catalog/ab27.jpg"),
                _row(767, "AL11", product_image="./catalog/al11.jpg"),
                _row(671, "AD61", product_details="LOUIS STYLE AD61 - Black frame."),
                _row(335, "AL74", product_name="BRABUS 8023",
                     product_slug="brabus-8023-al74",
                     product_image="catalog/opticalbazaar/AL74/AL74_4.jpg"),
            ]
            self.assertEqual(apply_script._changes(rows[2])["product_details"],
                             "OPTIWAR Browline AD61 - Black frame.")
            self.assertNotIn("product_details", apply_script._changes(
                _row(671, "AD61", product_details="OPTIWAR Browline AD61 - Black frame.")))
            dry_db = _DB()
            with patch.object(apply_script, "_load_rows", return_value=rows):
                apply_script.run(dry_db, types.SimpleNamespace(
                    apply=False, quarantine=False), str(image_root),
                    str(quarantine), str(restore_dir))
            self.assertEqual(dry_db.cursor_obj.queries, [])
            self.assertEqual(dry_db.commits, 0)
            self.assertEqual(dry_db.rollbacks, 1)

            db = _DB()
            with patch.object(apply_script, "_load_rows", return_value=rows):
                apply_script.run(db, types.SimpleNamespace(
                    apply=True, quarantine=True), str(image_root),
                    str(quarantine), str(restore_dir))
            self.assertEqual(db.commits, 1)
            self.assertGreaterEqual(len(db.cursor_obj.queries), 2)
            self.assertTrue(any("sell_on_in" in sql for sql, _ in db.cursor_obj.queries))
            self.assertFalse(source.exists())
            self.assertFalse(sticker_source.exists())
            self.assertEqual(static_status("/static/catalog/al11.jpg"), 404)
            restore_file = next(restore_dir.glob("brand_compliance_restore_*.json"))
            record = json.loads(restore_file.read_text())
            self.assertEqual(record["updates"]["668"]["sell_on_in"], 1)
            self.assertEqual(len(record["image_moves"]), 2)
            self.assertEqual(
                {Path(move["source"]) for move in record["image_moves"]},
                {source, sticker_source})
            quarantined = [Path(move["quarantined"])
                           for move in record["image_moves"]]
            self.assertTrue(all(path.exists() for path in quarantined))

            already_applied = [
                _row(668, "AB27", product_image="./catalog/ab27.jpg",
                     sell_on_in=0, sell_on_com=0, show_in_listings=0),
                _row(767, "AL11", product_image="./catalog/al11.jpg",
                     product_name=brand_compliance.RENAMED[767][1],
                     product_slug=brand_compliance.slugify(
                         brand_compliance.RENAMED[767][1]),
                     sell_on_in=0, sell_on_com=0, show_in_listings=0),
                _row(671, "AD61",
                     product_name=brand_compliance.RENAMED[671][1],
                     product_slug=brand_compliance.slugify(
                         brand_compliance.RENAMED[671][1])),
                _row(335, "AL74",
                     product_name=brand_compliance.RENAMED[335][1],
                     product_slug=brand_compliance.slugify(
                         brand_compliance.RENAMED[335][1]),
                     product_image=""),
            ]
            rerun_db = _DB()
            with patch.object(apply_script, "_load_rows",
                              return_value=already_applied):
                apply_script.run(rerun_db, types.SimpleNamespace(
                    apply=True, quarantine=False), str(image_root),
                    str(quarantine), str(restore_dir))
            self.assertEqual(rerun_db.cursor_obj.queries, [])
            self.assertEqual(rerun_db.commits, 0)

            restore_db = _DB()
            apply_script.restore(restore_db, str(restore_file),
                                 str(image_root), str(quarantine))
            self.assertEqual(restore_db.commits, 1)
            self.assertTrue(any("UPDATE products SET" in sql
                                for sql, _ in restore_db.cursor_obj.queries))
            self.assertTrue(source.exists())
            self.assertTrue(sticker_source.exists())
            self.assertEqual(static_status("/static/catalog/al11.jpg"), 200)
            self.assertTrue(all(not path.exists() for path in quarantined))

    def test_photo_remove_quarantines_only_the_selected_family_and_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_root = Path(tmp) / "static"
            quarantine = Path(tmp) / "quarantine"
            restore_dir = Path(tmp) / "restore"
            keep = Path("catalog/opticalbazaar/AL74/AL74_1.jpg")
            remove = Path("catalog/opticalbazaar/AL74/AL74_4.jpg")
            derivative = Path(
                "catalog/opticalbazaar/AL74/derivatives/AL74_4-200.webp")
            for relative, content in (
                    (keep, b"keep"), (remove, b"remove"),
                    (derivative, b"derivative")):
                path = image_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            row = _row(
                335, "AL74", product_name="BRABUS 8023",
                product_slug="brabus-8023-al74",
                product_image="./%s,./%s" % (keep.as_posix(), remove.as_posix()))
            plan = apply_script._photo_remove_plan(row)
            self.assertEqual(plan["image_value"], "./" + keep.as_posix())
            self.assertEqual(plan["paths"], [remove.as_posix()])

            dry_output = io.StringIO()
            with patch.object(apply_script, "_load_rows", return_value=[row]), \
                    redirect_stdout(dry_output):
                apply_script.run(
                    _DB(), types.SimpleNamespace(apply=False, quarantine=False),
                    str(image_root), str(quarantine), str(restore_dir))
            self.assertIn("DRY RUN PHOTO REMOVE 335 %s" % remove.as_posix(),
                          dry_output.getvalue())
            self.assertIn(
                "DRY RUN QUARANTINE %s" % (image_root / derivative),
                dry_output.getvalue())
            self.assertTrue((image_root / remove).exists())

            db = _DB()
            with patch.object(apply_script, "_load_rows", return_value=[row]):
                apply_script.run(
                    db, types.SimpleNamespace(apply=True, quarantine=True),
                    str(image_root), str(quarantine), str(restore_dir))
            self.assertFalse((image_root / remove).exists())
            self.assertTrue((image_root / keep).exists())
            self.assertFalse((image_root / derivative).exists())
            product_update = next(
                (sql, params) for sql, params in db.cursor_obj.queries
                if "product_image=%s" in sql)
            self.assertIn("./" + keep.as_posix(), product_update[1])
            restore_file = next(restore_dir.glob("brand_compliance_restore_*.json"))
            record = json.loads(restore_file.read_text())
            self.assertEqual(
                record["updates"]["335"]["product_image"],
                "./%s,./%s" % (keep.as_posix(), remove.as_posix()))
            self.assertEqual(len(record["image_moves"]), 2)

            apply_script.restore(
                _DB(), str(restore_file), str(image_root), str(quarantine))
            self.assertEqual((image_root / remove).read_bytes(), b"remove")
            self.assertEqual(
                (image_root / derivative).read_bytes(), b"derivative")

    def test_stock_set_requires_the_planned_from_quantity_and_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_root = Path(tmp) / "static"
            quarantine = Path(tmp) / "quarantine"
            restore_dir = Path(tmp) / "restore"
            row = _row(225, "BQ49", product_quantity=1)
            self.assertEqual(apply_script._changes(row)["product_quantity"], 0)
            self.assertNotIn(
                "product_quantity",
                apply_script._changes(dict(row, product_quantity=4)))
            self.assertNotIn(
                "product_quantity",
                apply_script._changes(dict(row, product_quantity=0)))

            dry_output = io.StringIO()
            with patch.object(
                    apply_script, "_load_rows",
                    return_value=[row, _already_named_photo_remove_row()]), \
                    redirect_stdout(dry_output):
                apply_script.run(
                    _DB(), types.SimpleNamespace(apply=False, quarantine=False),
                    str(image_root), str(quarantine), str(restore_dir))
            self.assertIn("stock updates=1", dry_output.getvalue())

            db = _DB()
            with patch.object(
                    apply_script, "_load_rows",
                    return_value=[row, _already_named_photo_remove_row()]):
                apply_script.run(
                    db, types.SimpleNamespace(apply=True, quarantine=True),
                    str(image_root), str(quarantine), str(restore_dir))
            self.assertTrue(any(
                "product_quantity" in sql and "order_status" not in sql
                for sql, _ in db.cursor_obj.queries))
            restore_file = next(restore_dir.glob("brand_compliance_restore_*.json"))
            record = json.loads(restore_file.read_text())
            self.assertEqual(record["updates"]["225"]["product_quantity"], 1)
            self.assertEqual(
                record["applied_values"]["225"]["product_quantity"], 0)
            restore_db = _DB()
            restore_db.cursor_obj.fetchone_result = {"product_quantity": 0}
            apply_script.restore(
                restore_db, str(restore_file), str(image_root), str(quarantine))
            self.assertTrue(any(
                "product_quantity" in sql and "order_status" not in sql
                for sql, _ in restore_db.cursor_obj.queries))

    def test_stock_restore_skips_quantity_changed_after_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_root = Path(tmp) / "static"
            quarantine = Path(tmp) / "quarantine"
            restore_dir = Path(tmp) / "restore"
            row = _row(225, "BQ49", product_quantity=1)
            with patch.object(
                    apply_script, "_load_rows",
                    return_value=[row, _already_named_photo_remove_row()]):
                apply_script.run(
                    _DB(), types.SimpleNamespace(apply=True, quarantine=True),
                    str(image_root), str(quarantine), str(restore_dir))
            restore_file = next(restore_dir.glob("brand_compliance_restore_*.json"))

            restore_db = _DB()
            restore_db.cursor_obj.fetchone_result = {"product_quantity": 4}
            output = io.StringIO()
            with redirect_stdout(output):
                apply_script.restore(
                    restore_db, str(restore_file),
                    str(image_root), str(quarantine))
            self.assertIn(
                "SKIP RESTORE product_quantity 225 (now 4, applied 0)",
                output.getvalue())
            product_update = next(
                (sql, params) for sql, params in restore_db.cursor_obj.queries
                if sql.startswith("UPDATE products SET") and params[-1] == 225)
            self.assertNotIn("product_quantity", product_update[0])
            self.assertIn("sell_on_in=%s", product_update[0])

    def test_stock_set_skips_a_quantity_recorded_in_a_prior_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_root = Path(tmp) / "static"
            quarantine = Path(tmp) / "quarantine"
            restore_dir = Path(tmp) / "restore"
            restore_dir.mkdir()
            prior_restore = restore_dir / "brand_compliance_restore_prior.json"
            prior_restore.write_text(
                json.dumps({"updates": {"225": {"product_quantity": 1}}}))
            row = _row(225, "BQ49", product_quantity=1)

            dry_output = io.StringIO()
            with patch.object(
                    apply_script, "_load_rows",
                    return_value=[row, _already_named_photo_remove_row()]), \
                    redirect_stdout(dry_output):
                apply_script.run(
                    _DB(), types.SimpleNamespace(apply=False, quarantine=False),
                    str(image_root), str(quarantine), str(restore_dir))
            self.assertIn("STOCK ALREADY APPLIED BQ49", dry_output.getvalue())
            self.assertIn("stock updates=0", dry_output.getvalue())

            db = _DB()
            with patch.object(
                    apply_script, "_load_rows",
                    return_value=[row, _already_named_photo_remove_row()]):
                apply_script.run(
                    db, types.SimpleNamespace(apply=True, quarantine=True),
                    str(image_root), str(quarantine), str(restore_dir))
            self.assertFalse(any(
                "product_quantity" in sql
                for sql, _ in db.cursor_obj.queries))
            new_restore = next(
                path for path in restore_dir.glob("brand_compliance_restore_*.json")
                if path != prior_restore)
            record = json.loads(new_restore.read_text())
            self.assertNotIn(
                "product_quantity", record["updates"].get("225", {}))

    def test_released_frame_republishes_relists_scrubs_seo_and_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_root = Path(tmp) / "static"
            quarantine = Path(tmp) / "quarantine"
            restore_dir = Path(tmp) / "restore"
            round1_restore = Path(tmp) / "round1.json"
            relative = Path("catalog/opticalbazaar/AB85/AB85_1.jpg")
            quarantined_source = quarantine / "20261123-163137" / relative
            quarantined_source.parent.mkdir(parents=True)
            quarantined_source.write_bytes(b"AB85 frame")
            public_source = image_root / relative
            public_source.parent.mkdir(parents=True)
            public_source.write_bytes(b"stale public copy")
            derivative_relative = Path(
                "catalog/opticalbazaar/AB85/derivatives/AB85_1-200.webp")
            public_derivative = image_root / derivative_relative
            public_derivative.parent.mkdir(parents=True)
            public_derivative.write_bytes(b"stale derivative")
            round1_restore.write_text(json.dumps({
                "image_moves": [{
                    "source": "/var/www/flaskr/static/" + relative.as_posix(),
                    "quarantined": str(quarantined_source),
                }],
            }))
            former = brand_compliance.FORMER_NAMES[696]
            row = _row(
                696, "AB85",
                product_image="./" + relative.as_posix(),
                product_name=former,
                product_slug="rb-ab85",
                product_details="%s AB85 frame." % former,
                description_seo="%s square eyeglasses." % former,
                sell_on_in=0, sell_on_com=0, show_in_listings=1)

            dry_db = _DB()
            output = io.StringIO()
            with patch.object(
                    apply_script, "_load_rows",
                    return_value=[row, _already_named_photo_remove_row()]), \
                    redirect_stdout(output):
                apply_script.run(
                    dry_db, types.SimpleNamespace(
                        apply=False, quarantine=False),
                    str(image_root), str(quarantine), str(restore_dir),
                    str(round1_restore))
            dry_output = output.getvalue()
            self.assertIn("DRY RUN REPUBLISH", dry_output)
            self.assertIn("SUMMARY off sale=0 relisted=1", dry_output)
            self.assertIn("renamed=1 seo-scrubbed=1", dry_output)
            self.assertIn("photos to quarantine=2 photos to republish=1",
                          dry_output)
            self.assertEqual(dry_db.cursor_obj.queries, [])
            self.assertEqual(public_source.read_bytes(), b"stale public copy")
            self.assertFalse(
                (image_root / "catalog" / "r2" / "AB85").exists())

            db = _DB()
            with patch.object(
                    apply_script, "_load_rows",
                    return_value=[row, _already_named_photo_remove_row()]):
                apply_script.run(
                    db, types.SimpleNamespace(apply=True, quarantine=True),
                    str(image_root), str(quarantine), str(restore_dir),
                    str(round1_restore))
            published = image_root / "catalog" / "r2" / "AB85" / "AB85_r2_01.jpg"
            self.assertEqual(published.read_bytes(), b"stale public copy")
            self.assertFalse(public_source.exists())
            self.assertFalse(public_derivative.exists())
            updates = next(
                params for sql, params in db.cursor_obj.queries
                if "UPDATE products SET" in sql)
            self.assertIn(1, updates)
            self.assertNotIn("show_in_listings", " ".join(
                sql for sql, _ in db.cursor_obj.queries))
            restore_file = next(restore_dir.glob("brand_compliance_restore_*.json"))
            record = json.loads(restore_file.read_text())
            saved = record["updates"]["696"]
            self.assertEqual(
                record["image_copies"][0]["source"], str(public_source))
            self.assertEqual(saved["description_seo"],
                             "%s square eyeglasses." % former)
            self.assertEqual(saved["product_image"], "./" + relative.as_posix())
            self.assertEqual(saved["sell_on_in"], 0)
            self.assertEqual(saved["sell_on_com"], 0)
            self.assertNotIn("show_in_listings", saved)
            self.assertEqual(len(record["image_copies"]), 1)
            self.assertEqual(len(record["image_moves"]), 2)

            restore_db = _DB()
            apply_script.restore(
                restore_db, str(restore_file), str(image_root), str(quarantine))
            self.assertFalse(published.exists())
            self.assertEqual(public_source.read_bytes(), b"stale public copy")
            self.assertEqual(public_derivative.read_bytes(), b"stale derivative")
            self.assertTrue(quarantined_source.exists())
            self.assertEqual(restore_db.commits, 1)

    def test_republish_quarantines_derivatives_when_master_is_already_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_root = Path(tmp) / "static"
            quarantine = Path(tmp) / "quarantine"
            restore_dir = Path(tmp) / "restore"
            round1_restore = Path(tmp) / "round1.json"
            relative = Path("catalog/opticalbazaar/AB85/AB85_1.jpg")
            quarantined_source = quarantine / "20261123-163137" / relative
            quarantined_source.parent.mkdir(parents=True)
            quarantined_source.write_bytes(b"approved frame")
            public_derivative_relative = Path(
                "catalog/opticalbazaar/AB85/derivatives/AB85_1-200.webp")
            public_derivative = image_root / public_derivative_relative
            public_derivative.parent.mkdir(parents=True)
            public_derivative.write_bytes(b"public derivative")
            round1_restore.write_text(json.dumps({
                "image_moves": [{
                    "source": "/var/www/flaskr/static/" + relative.as_posix(),
                    "quarantined": str(quarantined_source),
                }],
            }))
            row = _row(
                696, "AB85", product_name=brand_compliance.RENAMED[696][1],
                product_slug=brand_compliance.slugify(
                    brand_compliance.RENAMED[696][1]),
                product_image="./" + relative.as_posix(),
                sell_on_in=0, sell_on_com=0)

            output = io.StringIO()
            with patch.object(
                    apply_script, "_load_rows",
                    return_value=[row, _already_named_photo_remove_row()]), \
                    redirect_stdout(output):
                apply_script.run(
                    _DB(), types.SimpleNamespace(
                        apply=False, quarantine=False),
                    str(image_root), str(quarantine), str(restore_dir),
                    str(round1_restore))
            self.assertIn(
                "DRY RUN QUARANTINE %s -> %s" % (
                    public_derivative,
                    os.path.join(quarantine, "<timestamp>",
                                 public_derivative_relative.as_posix())),
                output.getvalue())

            db = _DB()
            with patch.object(
                    apply_script, "_load_rows",
                    return_value=[row, _already_named_photo_remove_row()]):
                apply_script.run(
                    db, types.SimpleNamespace(apply=True, quarantine=True),
                    str(image_root), str(quarantine), str(restore_dir),
                    str(round1_restore))
            self.assertFalse(public_derivative.exists())
            restore_file = next(restore_dir.glob("brand_compliance_restore_*.json"))
            record = json.loads(restore_file.read_text())
            self.assertEqual(len(record["image_moves"]), 1)
            self.assertEqual(len(record["image_copies"]), 1)

            apply_script.restore(
                _DB(), str(restore_file), str(image_root), str(quarantine))
            self.assertEqual(public_derivative.read_bytes(), b"public derivative")


if __name__ == "__main__":
    unittest.main()
