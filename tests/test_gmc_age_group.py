"""age_group in the Merchant feed, from an assignment or the kids flag.

29 offers were demoted in DE/FR/GB for missing_item_attribute_for_product_type
on age_group. The attribute was simply absent from the feed.

The first derivation read the product's words, which is how "BABY CAT 1402"
became ``infant``. Words are the wrong authority: the catalogue holds 13
``product_category_kids = 1`` frames, six of which say BABY and seven of which
are named LOUIS STYLE K-800xA, and the choice between newborn, infant, toddler
and kids is an intended age range that neither a name nor a frame size states.
So a children's frame carries the age somebody assigned it, or no attribute at
all.

    python3 -m unittest tests.test_gmc_age_group
"""
import ast
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from catalogue import AGE_GROUPS, GMC_COLUMNS, age_group  # noqa: E402


class AgeGroupTests(unittest.TestCase):
    def test_an_ordinary_frame_is_adult(self):
        """The 29 demoted offers are ordinary spectacle frames: adult is read
        from the kids flag, not defaulted."""
        for code in ("AB17", "AM61", "BQ50"):
            self.assertEqual(age_group({
                "product_code": code, "product_name": "OPTIWAR " + code,
                "product_gender": "Unisex",
                "product_category_kids": 0}), "adult")

    def test_a_kids_frame_with_no_assignment_emits_nothing(self):
        """AH98 (BABY CAT 1402) and AH08 (LOUIS STYLE K-8003A) are both
        product_category_kids = 1. Neither may be guessed at, and a demotion is
        a smaller harm than a child's frame labelled adult."""
        for code, name in (("AH98", "BABY CAT 1402"),
                           ("AH08", "LOUIS STYLE K-8003A")):
            self.assertEqual(age_group({
                "product_code": code, "product_name": name,
                "product_gender": "Unisex",
                "product_category_kids": 1}), "",
                "%s must not be classified without an assignment" % code)

    def test_an_assignment_is_what_makes_a_kids_frame_emit(self):
        for value in AGE_GROUPS:
            self.assertEqual(age_group({
                "product_code": "AH98", "product_name": "BABY CAT 1402",
                "product_category_kids": 1,
                "gmc_age_group": value}), value)

    def test_an_assignment_is_normalised_and_validated(self):
        self.assertEqual(age_group({"product_category_kids": 1,
                                    "gmc_age_group": " Toddler "}), "toddler")
        # Not one of Google's five: Google would reject the attribute, so the
        # product falls back to having no assignment rather than sending it.
        self.assertEqual(age_group({"product_category_kids": 1,
                                    "gmc_age_group": "babies"}), "")
        self.assertEqual(age_group({"product_category_kids": 0,
                                    "gmc_age_group": "babies"}), "adult")

    def test_words_are_not_consulted_at_all(self):
        """A name that reads as a child's frame does not make one, and a name
        that reads as an adult's does not unmake one."""
        self.assertEqual(age_group({
            "product_name": "OPTIWAR BABY FRAME",
            "product_details": "for children",
            "product_category_kids": 0}), "adult")
        self.assertEqual(age_group({
            "product_name": "LOUIS STYLE K-8003A",
            "product_category_kids": 1}), "")

    def test_a_flag_that_was_not_selected_is_not_evidence_of_adult(self):
        """The lesson of the lens-vertical defect: a row-level decision on a
        column the query never fetched must say nothing, not say no."""
        self.assertEqual(age_group({"product_name": "OPTIWAR AB17"}), "")
        self.assertEqual(age_group({"product_category_kids": None}), "")
        self.assertEqual(age_group({"product_category_kids": "yes"}), "")
        self.assertEqual(age_group({}), "")
        self.assertEqual(age_group(None), "")

    def test_the_feed_query_fetches_what_the_decision_reads(self):
        """Same class of defect as the lens guard: the derivation is silently
        wrong if the SELECT omits the columns it decides on."""
        with open(os.path.join(REPO, "models.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("<g:age_group>", src)
        self.assertIn("age_group(p)", src)
        feed = src.split("def google_merchant_feed", 1)
        self.assertEqual(len(feed), 2, "feed generator was renamed")
        query = feed[1].split("FROM products", 1)[0]
        for column in ("product_category_kids", "gmc_age_group"):
            self.assertIn(column, query,
                          "the feed query must select " + column)

    def test_the_column_is_declared_for_the_migration(self):
        """deploy.py reads GMC_COLUMNS out of this module's source, so the
        column cannot be emitted by code the schema does not carry."""
        self.assertEqual(GMC_COLUMNS, (("gmc_age_group", "VARCHAR(20) NULL"),))
        with open(os.path.join(REPO, "deploy", "deploy.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        self.assertIn("catalogue_columns", names)


if __name__ == "__main__":
    unittest.main()
