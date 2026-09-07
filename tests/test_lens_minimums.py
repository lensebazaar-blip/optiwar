"""The catalogue-wide minimum-order seed, and the one rule it is held to.

    python3 -m unittest tests.test_lens_minimums
"""
import csv
import importlib.util
import json
import os
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lens_minimums = _load("lens_minimums_under_test",
                      os.path.join(REPO, "lens_minimums.py"))
cl_import = _load("cl_import_for_minimums", os.path.join(REPO, "cl_import.py"))


def _seed_file(products):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump({"_meta": {"source": "test"}, "products": products}, fh)
    return path


class TheRule(unittest.TestCase):
    def test_both_per_eye_is_ceil_of_half(self):
        self.assertEqual(lens_minimums.both_per_eye(12), 6)
        self.assertEqual(lens_minimums.both_per_eye(5), 3)
        self.assertEqual(lens_minimums.both_per_eye(9), 5)
        self.assertEqual(lens_minimums.both_per_eye(1), 1)

    def test_a_zero_or_negative_minimum_is_refused(self):
        for bad in (0, -1):
            with self.assertRaises(lens_minimums.SeedError):
                lens_minimums.both_per_eye(bad)


class TheCommittedSeed(unittest.TestCase):
    def setUp(self):
        self.rows = lens_minimums.rows()

    def test_all_59_models_are_stated_once(self):
        self.assertEqual(len(self.rows), 59)
        self.assertEqual(len({r["model_key"] for r in self.rows}), 59)

    def test_every_row_obeys_the_rule(self):
        for row in self.rows:
            self.assertEqual(row["min_both_per_eye"],
                             lens_minimums.both_per_eye(row["min_single_eye"]),
                             row["model"])
            # The combined both-eye minimum is therefore always even.
            self.assertEqual((2 * row["min_both_per_eye"]) % 2, 0)

    def test_precision1_is_12_single_and_6_per_eye(self):
        self.assertEqual(lens_minimums.minimums_for(self.rows, "Precision 1"),
                         (12, 6))
        # Case and punctuation are not identity; a different name is.
        self.assertEqual(lens_minimums.minimums_for(self.rows, "PRECISION  1"),
                         (12, 6))
        self.assertIsNone(lens_minimums.minimums_for(self.rows, "PRECISION1"))

    def test_soflens_dailies_toric_rounds_5_up_to_3_per_eye(self):
        found = [r for r in self.rows
                 if r["model_key"].startswith("soflens-dailies") and
                 "toric" in r["model_key"]]
        self.assertEqual(len(found), 1, [r["model"] for r in self.rows])
        self.assertEqual((found[0]["min_single_eye"],
                          found[0]["min_both_per_eye"]), (5, 3))

    def test_the_old_both_column_is_kept_only_as_provenance(self):
        differing = [r for r in self.rows
                     if r["source_both_value"] not in
                     (None, r["min_both_per_eye"])]
        self.assertTrue(differing)
        for row in differing:
            # Every one is an odd single minimum the old table halved down.
            self.assertEqual(row["min_single_eye"] % 2, 1, row["model"])
            self.assertEqual(row["source_both_value"],
                             row["min_single_eye"] // 2, row["model"])

    def test_an_unknown_model_has_no_minimum(self):
        self.assertIsNone(lens_minimums.minimums_for(self.rows, "Nothing"))


class ASeedThatBreaksTheRule(unittest.TestCase):
    def test_a_stated_both_that_disagrees_without_a_note_is_refused(self):
        path = _seed_file([{"model": "X", "min_single_eye_boxes": 9,
                            "min_both_eyes_boxes_per_eye": 4}])
        with self.assertRaises(lens_minimums.SeedError):
            lens_minimums.rows(path)

    def test_a_documented_exception_is_kept_as_stated(self):
        path = _seed_file([{"model": "X", "min_single_eye_boxes": 9,
                            "min_both_eyes_boxes_per_eye": 4,
                            "exception_note": "supplier terms 2026-09"}])
        (row,) = lens_minimums.rows(path)
        self.assertEqual(row["min_both_per_eye"], 4)
        self.assertEqual(row["exception_note"], "supplier terms 2026-09")

    def test_a_missing_single_minimum_is_refused(self):
        path = _seed_file([{"model": "X", "min_both_eyes_boxes_per_eye": 4}])
        with self.assertRaises(lens_minimums.SeedError):
            lens_minimums.rows(path)


class _Recorder(object):
    def __init__(self, answers=()):
        self.statements, self._answers = [], list(answers)

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchall(self):
        return self._answers.pop(0) if self._answers else []


class Seeding(unittest.TestCase):
    def test_every_row_is_upserted_and_the_table_created_first(self):
        cursor = _Recorder()
        lens_minimums.ensure_table(cursor)
        written = lens_minimums.seed(cursor, lens_minimums.rows())
        self.assertEqual(written, 59)
        self.assertIn("CREATE TABLE IF NOT EXISTS contact_lens_min_order",
                      cursor.statements[0][0])
        upserts = [s for s, _ in cursor.statements[1:]]
        self.assertEqual(len(upserts), 59)
        self.assertTrue(all("ON DUPLICATE KEY UPDATE" in s for s in upserts))

    def test_profiles_naming_a_model_receive_its_minimums(self):
        cursor = _Recorder(answers=[
            [{"product_id": 1015, "min_order_model": "Precision 1"},
             {"product_id": 1016, "min_order_model": "No Such Lens"}],
            [{"model_key": "precision-1", "min_single_eye": 12,
              "min_both_per_eye": 6}],
        ])
        matched, unmatched = lens_minimums.apply_to_products(cursor, apply=True)
        self.assertEqual(matched, [(1015, "Precision 1", 12, 6)])
        self.assertEqual(unmatched, [(1016, "No Such Lens")])
        updates = [(s, p) for s, p in cursor.statements if s.startswith("UPDATE")]
        self.assertEqual(updates[0][1], (12, 6, 1015))
        self.assertEqual(len(updates), 1)


class TheImportSheet(unittest.TestCase):
    def _precision1_row(self):
        path = os.path.join(REPO, "lens_data", "PRECISION1", "products.csv")
        with open(path, newline="", encoding="utf-8") as fh:
            (row,) = list(csv.DictReader(fh))
        return row

    def test_precision1_imports_at_12_and_6_from_the_seed(self):
        parsed = cl_import.parse_product(self._precision1_row())
        self.assertEqual(parsed["min_order_model"], "Precision 1")
        self.assertEqual(parsed["min_boxes_single_eye"], 12)
        self.assertEqual(parsed["min_boxes_both_per_eye"], 6)

    def test_a_sheet_value_that_disagrees_with_the_seed_is_refused(self):
        row = self._precision1_row()
        row["min_boxes_both_per_eye"] = "5"
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_product(row)

    def test_a_sheet_naming_a_model_the_seed_lacks_is_refused(self):
        row = self._precision1_row()
        row["min_order_model"] = "Not A Lens"
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_product(row)

    def test_a_sheet_stating_its_own_single_minimum_derives_both(self):
        row = self._precision1_row()
        row.update(min_order_model="", min_boxes_single_eye="9",
                   min_boxes_both_per_eye="")
        parsed = cl_import.parse_product(row)
        self.assertEqual((parsed["min_boxes_single_eye"],
                          parsed["min_boxes_both_per_eye"]), (9, 5))
        row["min_boxes_both_per_eye"] = "4"
        with self.assertRaises(cl_import.RowError):
            cl_import.parse_product(row)


if __name__ == "__main__":
    unittest.main()
