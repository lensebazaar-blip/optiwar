"""Regression tests for optiwar.com (global/EUR) lens-content localization.

The lens content pages (`/lenses`, `/lenses/<slug>`) are served from the
`LENS_DATA` dict, which is authored in INR/India copy for optiwar.in. On the
global site (optiwar.com) that copy must be converted to EUR/worldwide so no
"Rs ..." price or "Free shipping India" phrasing leaks onto the .com storefront.

These tests exercise the real `_localize_lens_for_global` helper and the real
`LENS_DATA` extracted from `models.py` (without importing the full Flask app, so
no DB/payment deps are needed) and assert:

  - every customer-facing field is free of `Rs <n>` / India-shipping phrasing on .com;
  - a representative INR add-on tier is converted to its EUR equivalent;
  - the India-site copy is left completely unchanged.

    python3 -m unittest tests.test_lens_localization
"""
import ast
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(REPO, "models.py")


def _load_from_models():
    """Extract LENS_DATA and _localize_lens_for_global from models.py source
    without importing the module (avoids Flask/DB import side effects)."""
    src = open(MODELS, encoding="utf-8").read()

    i = src.index("LENS_DATA = {")
    j = src.index("\ndef _localize_lens_for_global", i)
    dict_text = src[i:j]
    dict_text = dict_text[dict_text.index("{"): dict_text.rindex("}") + 1]
    lens_data = ast.literal_eval(dict_text)

    fi = src.index("def _localize_lens_for_global(")
    fj = src.index("\n@bp.route('/lenses')", fi)
    ns = {}
    exec(src[fi:fj], ns)  # noqa: S102 - trusted first-party source
    return lens_data, ns["_localize_lens_for_global"]


LENS_DATA, localize = _load_from_models()
# EUR add-on tiers keyed by the INR price (mirrors the lens_pricing.json map).
EUR_MAP = {50: "5", 100: "7", 200: "7", 250: "8", 350: "10",
           500: "8", 650: "10", 800: "10", 1000: "10"}
_LEAK = re.compile(r"Rs ?[0-9]|\u20b9|\bIndia\b", re.I)


def _walk_strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, list):
        for e in x:
            yield from _walk_strings(e)
    elif isinstance(x, dict):
        for k, e in x.items():
            if k == "slug":  # slugs are URL identifiers, never displayed as copy
                continue
            yield from _walk_strings(e)


class GlobalLensLocalizationTests(unittest.TestCase):
    def test_no_inr_or_india_leaks_on_com(self):
        for slug, entry in LENS_DATA.items():
            localized = localize(entry, EUR_MAP)
            leaks = [s for s in _walk_strings(localized) if _LEAK.search(s)]
            self.assertEqual(
                leaks, [],
                msg=f"{slug}: INR/India copy leaked onto optiwar.com: {leaks}",
            )

    def test_known_tier_converted_to_eur(self):
        # bifocal-round-lenses benefits carry "just Rs 250" -> "just \u20ac8"
        localized = localize(LENS_DATA["bifocal-round-lenses"], EUR_MAP)
        joined = " ".join(_walk_strings(localized))
        self.assertIn("\u20ac", joined)
        self.assertNotIn("Rs 250", joined)

    def test_price_eur_is_set(self):
        localized = localize(LENS_DATA["anti-glare-lenses"], EUR_MAP)
        self.assertEqual(localized["price_eur"], EUR_MAP[50])

    def test_india_copy_unchanged(self):
        # The helper must not mutate the source dict; India site uses the raw copy.
        import copy
        before = copy.deepcopy(LENS_DATA["anti-glare-lenses"])
        localize(LENS_DATA["anti-glare-lenses"], EUR_MAP)
        self.assertEqual(LENS_DATA["anti-glare-lenses"], before)
        # And the raw India copy still contains its INR phrasing.
        self.assertIn("Rs 50", before["price_label"])


if __name__ == "__main__":
    unittest.main()
