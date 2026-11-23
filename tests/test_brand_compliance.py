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
                         "OPTIWAR Round Metal AL69")
        self.assertEqual(brand_compliance.RENAMED[671][1],
                         "OPTIWAR Browline AD61")

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
    @classmethod
    def setUpClass(cls):
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

    def test_held_product_page_is_not_found(self):
        product = _row(668, "AB27", product_category="Spectacles Frame",
                       product_slug="ray-ban-ab27", product_status="ACTIVE",
                       product_vertical="EYEWEAR")
        with patch.object(self.models, "get_db", return_value=_ProductDB(product)):
            response = self.app.test_client().get(
                "/categories/spectacles-frame/ray-ban-ab27?pid=668")
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
        "product_image": "",
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
                _row(671, "AD61"),
            ]
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
