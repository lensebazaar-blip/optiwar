import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import brand_compliance

spec = importlib.util.spec_from_file_location(
    "brand_compliance_apply_test", ROOT / "scripts" / "brand_compliance_apply.py")
apply_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply_script)


class ComplianceDataTest(unittest.TestCase):
    def test_settled_counts_keep_ids_and_spot_checks(self):
        self.assertEqual(len(brand_compliance.HELD), 110)
        self.assertEqual(len(brand_compliance.RENAMED), 32)
        self.assertEqual(brand_compliance.KEEP_ON_SALE, {1: "BB44", 362: "AK98"})
        self.assertTrue(set(brand_compliance.KEEP_ON_SALE).isdisjoint(
            brand_compliance.HELD))
        self.assertEqual(brand_compliance.HELD[668],
                         ("AB27", "Ray-Ban", "frame_mark"))
        self.assertEqual(brand_compliance.HELD[332][0:1], ("AL69",))
        self.assertEqual(brand_compliance.HELD[332][2], "lens_sticker")
        self.assertEqual(brand_compliance.RENAMED[332][1],
                         "OPTIWAR Round Metal")
        self.assertEqual(brand_compliance.RENAMED[671][1],
                         "OPTIWAR Browline")

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
            cleaned = brand_compliance.scrub_text("%s X - frame." % former, product_id)
            self.assertNotIn(former.lower(), cleaned.lower(), former)

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
        self.assertTrue(brand_compliance.is_held(668))
        self.assertTrue(brand_compliance.is_held({"product_id": 668}))
        for site in (catalogue.SITE_COM, catalogue.SITE_IN):
            self.assertFalse(catalogue.is_product_allowed(668, site))
            self.assertTrue(catalogue.is_product_allowed(
                {"product_id": 1, "product_vertical": "EYEWEAR"}, site))

    def test_matcher_and_public_style_sources_exclude_old_labels(self):
        from tests.test_frame_offers import offers
        self.assertIn("668", offers.match_sql("optiwar.com"))
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
        from flask import Flask
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
                        self.assertIn("/%s?pid=671" % new_slug, body)
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


class _Cursor:
    def __init__(self):
        self.queries = []

    def execute(self, sql, params=()):
        self.queries.append((sql, params))


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


class ApplyScriptTest(unittest.TestCase):
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
            sticker_source = image_root / "catalog" / "al69.jpg"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"frame")
            sticker_source.write_bytes(b"lens sticker")
            quarantine = Path(tmp) / "quarantine"
            restore_dir = Path(tmp) / "restore"
            rows = [
                _row(668, "AB27", product_image="./catalog/ab27.jpg"),
                _row(332, "AL69", product_image="./catalog/al69.jpg"),
                _row(671, "AD61", product_details="LOUIS STYLE AD61 - Black frame."),
            ]
            self.assertEqual(apply_script._changes(rows[2])["product_details"],
                             "OPTIWAR Browline AD61 - Black frame.")
            self.assertNotIn("product_details", apply_script._changes(
                _row(671, "AD61", product_details="OPTIWAR Browline AD61 - Black frame.")))
            dry_db = _DB()
            with patch.object(apply_script, "_load_rows", return_value=rows):
                apply_script.run(dry_db, types.SimpleNamespace(
                    apply=False, quarantine=False))
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
                _row(332, "AL69", product_image="./catalog/al69.jpg",
                     product_name=brand_compliance.RENAMED[332][1],
                     product_slug=brand_compliance.slugify(
                         brand_compliance.RENAMED[332][1]),
                     sell_on_in=0, sell_on_com=0, show_in_listings=0),
                _row(671, "AD61",
                     product_name=brand_compliance.RENAMED[671][1],
                     product_slug=brand_compliance.slugify(
                         brand_compliance.RENAMED[671][1])),
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
            self.assertTrue(all(not path.exists() for path in quarantined))


if __name__ == "__main__":
    unittest.main()
