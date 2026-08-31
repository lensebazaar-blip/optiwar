"""age_group in the Merchant feed, derived from the catalogue.

29 offers were demoted in DE/FR/GB for missing_item_attribute_for_product_type
on age_group. The attribute was simply absent from the feed. It is now derived
from the product's own words, so a kids frame is labelled kids — sending
"adult" for the whole catalogue would have cleared the demotion while
mislabelling every child's frame we ever list.

    python3 -m unittest tests.test_gmc_age_group
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from embed_helper import age_group  # noqa: E402


class AgeGroupTests(unittest.TestCase):
    def test_an_ordinary_unisex_frame_is_adult(self):
        self.assertEqual(age_group({
            "product_name": "OPTIWAR AB17", "product_gender": "Unisex",
            "product_category": "Spectacles Frame"}), "adult")

    def test_a_kids_frame_is_not_labelled_adult(self):
        for field, value in (("product_name", "OPTIWAR KIDS FRAME"),
                             ("product_category", "Kids Spectacles"),
                             ("product_gender", "Boys"),
                             ("product_details", "Flexible frame for children")):
            self.assertEqual(age_group({field: value}), "kids",
                             "%s=%r should classify as kids" % (field, value))

    def test_the_six_real_baby_frames_are_not_called_adult(self):
        """BB44/BB42/AI00/AK98/AH98/AH99 are BABY frames carrying gender
        Unisex: gender alone would have made them adult."""
        self.assertEqual(age_group({
            "product_code": "BB44", "product_name": "OPTIWAR BABY FRAME",
            "product_category": "Spectacles Frame",
            "product_gender": "Unisex"}), "infant")

    def test_the_narrowest_stated_age_wins(self):
        self.assertEqual(age_group({"product_name": "NEWBORN KIDS"}),
                         "newborn")
        self.assertEqual(age_group({"product_name": "TODDLER GIRLS"}),
                         "toddler")

    def test_a_demoted_frame_with_no_age_signal_is_adult(self):
        """The 29 demoted offers are Unisex spectacle frames with nothing
        childlike in the catalogue — for those, adult is the read, not a
        default."""
        for code in ("AB17", "AM61", "BQ50"):
            self.assertEqual(age_group({
                "product_code": code, "product_name": "OPTIWAR " + code,
                "product_category": "Spectacles Frame",
                "product_gender": "Unisex"}), "adult")

    def test_a_word_that_merely_contains_kid_is_not_a_kids_frame(self):
        """'Aviator' style names and colours must not trip the match."""
        self.assertEqual(age_group({
            "product_name": "OPTIWAR CHILDISHLY BOLD",
            "product_details": "Kidney-shaped acetate"}), "adult")

    def test_nothing_to_read_stays_empty_so_the_demotion_stays_visible(self):
        self.assertEqual(age_group({}), "")
        self.assertEqual(age_group(None), "")
        self.assertEqual(age_group({"product_name": ""}), "")

    def test_feed_emits_the_attribute(self):
        """The derivation is useless if the feed never writes the tag."""
        with open(os.path.join(REPO, "models.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("<g:age_group>", src)
        self.assertIn("age_group(p)", src)


if __name__ == "__main__":
    unittest.main()
