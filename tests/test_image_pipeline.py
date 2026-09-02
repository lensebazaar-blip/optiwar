"""What the image pipeline may and may not do to a photograph.

The rules asserted here: a recipe that does not describe a buildable product is
refused; a master is square, sRGB and never larger than the photograph allows;
the ladder stops at the master rather than upscaling; JPEG is always produced
so a customer cannot meet a broken image; a re-run is byte-identical; the
manifest keeps one hash per file; a view nobody photographed is a warning, not
an invented picture; and building images releases nothing.

    python3 -m unittest tests.test_image_pipeline
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name + "_under_test", os.path.join(REPO, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


image_pipeline = _load("image_pipeline")

RECIPE = {
    "product": "TESTPACK",
    "catalog_dir": "contact-lenses/TESTPACK",
    "master_px": 400,
    "views": [
        {"code": "01_hero", "view": "front", "source": "front.jpg",
         "primary": True, "alt": "front of the box"},
        {"code": "04_rear", "view": "rear", "source": "rear.jpg",
         "rotate": 270, "alt": "rear of the box", "gmc": False},
    ],
    "missing_views": [{"code": "02_angle", "reason": "not photographed"}],
}


def carton(path, size=(600, 900)):
    """A photograph-shaped stand-in: a coloured panel on a wooden surface."""
    img = Image.new("RGB", size, (122, 96, 62))
    panel = Image.new("RGB", (size[0] // 2, size[1] // 2), (30, 90, 200))
    img.paste(panel, (size[0] // 4, size[1] // 4))
    img.save(path, "JPEG", quality=95)


class Recipes(unittest.TestCase):

    def test_a_product_has_exactly_one_primary_image(self):
        recipe = dict(RECIPE, views=[dict(RECIPE["views"][0], primary=False),
                                     dict(RECIPE["views"][1])])
        with self.assertRaises(image_pipeline.RecipeError):
            image_pipeline.validate_recipe(recipe)

    def test_two_primaries_is_also_refused(self):
        recipe = dict(RECIPE, views=[dict(RECIPE["views"][0]),
                                     dict(RECIPE["views"][1], primary=True)])
        with self.assertRaises(image_pipeline.RecipeError):
            image_pipeline.validate_recipe(recipe)

    def test_a_view_without_alt_text_is_refused(self):
        views = [dict(RECIPE["views"][0]), dict(RECIPE["views"][1])]
        del views[1]["alt"]
        with self.assertRaises(image_pipeline.RecipeError):
            image_pipeline.validate_recipe(dict(RECIPE, views=views))

    def test_a_code_stated_twice_is_refused(self):
        views = [dict(RECIPE["views"][0]),
                 dict(RECIPE["views"][1], code="01_hero")]
        with self.assertRaises(image_pipeline.RecipeError):
            image_pipeline.validate_recipe(dict(RECIPE, views=views))

    def test_an_empty_crop_is_refused(self):
        views = [dict(RECIPE["views"][0], crop=[10, 10, 0, 50]),
                 dict(RECIPE["views"][1])]
        with self.assertRaises(image_pipeline.RecipeError):
            image_pipeline.validate_recipe(dict(RECIPE, views=views))

    def test_the_shipped_precision1_recipe_is_buildable(self):
        recipe = image_pipeline.load_recipe(
            os.path.join(REPO, "image_recipes", "PRECISION1.json"))
        self.assertEqual(recipe["master_px"], 2000)
        self.assertEqual(sum(1 for v in recipe["views"] if v.get("primary")), 1)
        # The 3/4 angle is declared missing rather than built from a face.
        self.assertEqual([m["code"] for m in recipe["missing_views"]],
                         ["02_angle"])


class Masters(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(self.src)
        carton(os.path.join(self.src, "front.jpg"))
        carton(os.path.join(self.src, "rear.jpg"), (900, 600))
        self.catalog = os.path.join(self.tmp, "catalog")

    def build(self, recipe=None, apply=True):
        return image_pipeline.build_product(
            image_pipeline.validate_recipe(dict(recipe or RECIPE)),
            self.src, self.catalog, apply=apply)

    def test_a_master_is_square_rgb_and_not_upscaled(self):
        _, written, _ = self.build()
        master = os.path.join(self.catalog, "contact-lenses/TESTPACK",
                              "01_hero.jpg")
        self.assertIn(master, written)
        with Image.open(master) as img:
            self.assertEqual(img.mode, "RGB")
            self.assertEqual(img.size[0], img.size[1])
            self.assertLessEqual(img.size[0], 400)

    def test_the_ladder_stops_at_the_master_and_always_has_jpeg(self):
        _, written, _ = self.build()
        names = {os.path.basename(p) for p in written}
        self.assertIn("01_hero-200.jpg", names)
        # master_px is 400, so a 800/1200/2000 rung would be invented pixels.
        self.assertFalse([n for n in names
                          if n.startswith("01_hero-800")
                          or n.startswith("01_hero-1200")
                          or n.startswith("01_hero-2000")])
        for width in (200, 400):
            self.assertIn("01_hero-%d.jpg" % width, names)

    def test_a_rerun_writes_the_same_bytes(self):
        _, written, _ = self.build()
        first = {p: image_pipeline.sha256(p) for p in written}
        self.build()
        self.assertEqual(first, {p: image_pipeline.sha256(p) for p in written})

    def test_a_dry_run_writes_nothing(self):
        _, written, _ = self.build(apply=False)
        self.assertTrue(written)
        self.assertFalse(os.path.exists(self.catalog))

    def test_a_missing_photograph_blocks_instead_of_inventing_one(self):
        os.remove(os.path.join(self.src, "rear.jpg"))
        _, _, warnings = self.build()
        self.assertIn(("BLOCK", "source photograph missing: rear.jpg"),
                      warnings)

    def test_a_view_nobody_photographed_is_a_warning_not_a_block(self):
        _, _, warnings = self.build()
        levels = {level for level, _ in warnings}
        self.assertEqual(levels, {"WARN"})
        self.assertIn("02_angle", warnings[0][1])

    def test_source_photographs_are_never_modified(self):
        before = {name: image_pipeline.sha256(os.path.join(self.src, name))
                  for name in os.listdir(self.src)}
        self.build()
        after = {name: image_pipeline.sha256(os.path.join(self.src, name))
                 for name in os.listdir(self.src)}
        self.assertEqual(before, after)


class Records(unittest.TestCase):

    def test_the_primary_leads_and_every_record_names_its_photograph(self):
        recipe = image_pipeline.validate_recipe(dict(
            RECIPE, views=[dict(RECIPE["views"][1]),
                           dict(RECIPE["views"][0])]))
        records = image_pipeline.image_records(recipe)
        self.assertTrue(records[0]["is_primary"])
        self.assertEqual(records[0]["code"], "01_hero")
        for record in records:
            self.assertTrue(record["source_file"])
            self.assertEqual(record["source"], "OPTIWAR_ORIGINAL_PHOTOGRAPH")
            self.assertEqual(record["processing"], "DETERMINISTIC_APPROVED")
            self.assertTrue(record["path"].startswith(recipe["catalog_dir"]))

    def test_a_view_may_be_kept_out_of_the_merchant_feed(self):
        records = image_pipeline.image_records(
            image_pipeline.validate_recipe(dict(RECIPE)))
        gmc = {r["code"]: r["gmc"] for r in records}
        self.assertTrue(gmc["01_hero"])
        self.assertFalse(gmc["04_rear"])

    def test_no_primary_image_blocks_release(self):
        records = [dict(image_pipeline.image_records(
            image_pipeline.validate_recipe(dict(RECIPE)))[0], is_primary=False)]
        warnings = image_pipeline.qa_warnings({"missing_views": []}, records)
        self.assertIn(("BLOCK", "no primary image"), warnings)


class Manifest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "derivative_manifest.tsv")

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return [line.rstrip("\n").split("\t") for line in fh if line.strip()]

    def test_a_rebuilt_file_replaces_its_row_rather_than_adding_one(self):
        image_pipeline.merge_manifest(self.path, [("a/x.jpg", "1" * 64),
                                                  ("a/y.jpg", "2" * 64)])
        image_pipeline.merge_manifest(self.path, [("a/x.jpg", "3" * 64)])
        rows = self.read()
        self.assertEqual([r[0] for r in rows], ["a/x.jpg", "a/y.jpg"])
        self.assertEqual(dict(rows)["a/x.jpg"], "3" * 64)

    def test_other_products_survive_a_rebuild(self):
        image_pipeline.merge_manifest(self.path, [("frames/BB02_1.jpg", "9" * 64)])
        image_pipeline.merge_manifest(self.path, [("a/x.jpg", "1" * 64)])
        self.assertIn("frames/BB02_1.jpg", dict(self.read()))


if __name__ == "__main__":
    unittest.main()
