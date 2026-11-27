import unittest

from india_states import (
    ALIASES, TALLY_STATES, canonical_state, require_india_state, state_for_site,
    template_context,
)
from eu_ops.services import india_states as eu_india_states


class IndiaStatesTest(unittest.TestCase):
    def test_canonical_state_accepts_names_aliases_and_codes(self):
        cases = (
            ("Chattisgarh", "Chhattisgarh"),
            ("chhatisgarh", "Chhattisgarh"),
            ("CG", "Chhattisgarh"),
            ("Up", "Uttar Pradesh"),
            ("Orissa", "Odisha"),
            ("New Delhi", "Delhi"),
            ("Jammu and Kashmir", "Jammu & Kashmir"),
            ("The Dadra and Nagar Haveli and Daman and Diu",
             "Dadra & Nagar Haveli and Daman & Diu"),
            ("Dadra and Nagar Haveli and Daman and Diu",
             "Dadra & Nagar Haveli and Daman & Diu"),
            ("West Bengal", "West Bengal"),
            ("  west   Bengal ", "West Bengal"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(canonical_state(text), expected)
        self.assertIsNone(canonical_state("Zz"))

    def test_require_india_state_accepts_india_country_aliases(self):
        for country in ("", "India", "IN", "Bharat", " india ", " in ", "bHaRaT"):
            with self.subTest(country=country):
                self.assertEqual(require_india_state("Chattisgarh", country),
                                 "Chhattisgarh")

    def test_state_list_has_exactly_the_tally_names(self):
        self.assertEqual(
            TALLY_STATES,
            (
                "Andaman & Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh",
                "Assam", "Bihar", "Chandigarh", "Chhattisgarh",
                "Dadra & Nagar Haveli and Daman & Diu", "Delhi", "Goa", "Gujarat",
                "Haryana", "Himachal Pradesh", "Jammu & Kashmir", "Jharkhand",
                "Karnataka", "Kerala", "Ladakh", "Lakshadweep", "Madhya Pradesh",
                "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland",
                "Odisha", "Puducherry", "Punjab", "Rajasthan", "Sikkim",
                "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh",
                "Uttarakhand", "West Bengal",
            ),
        )

    def test_unknown_india_state_fails_closed(self):
        with self.assertRaisesRegex(
                ValueError, "Please choose your state from the list"):
            require_india_state("Zz", "India")

    def test_non_india_country_and_site_preserve_free_text(self):
        self.assertEqual(require_india_state("Île-de-France", "France"),
                         "Île-de-France")
        self.assertEqual(state_for_site("Île-de-France", "France", False),
                         "Île-de-France")

    def test_india_site_canonicalizes_address_state(self):
        self.assertEqual(state_for_site("Chattisgarh", "India", True),
                         "Chhattisgarh")
        self.assertEqual(template_context("Chattisgarh")["prefill_state"],
                         "Chhattisgarh")
        self.assertEqual(template_context("Zz")["prefill_state"], "")

    def test_eu_ops_copy_has_identical_states_and_aliases(self):
        self.assertEqual(eu_india_states.TALLY_STATES, TALLY_STATES)
        self.assertEqual(eu_india_states.ALIASES, ALIASES)


if __name__ == "__main__":
    unittest.main()
