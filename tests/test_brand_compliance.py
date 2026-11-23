"""Frames held off sale for trademark reasons, and the retired style names.

The brand audit of 23 Nov 2026 found other companies' logos in the photos of
live frames. Held frames must vanish from every surface on both sites, the two
frames the owner kept (BB44, AK98) must stay, and "Clubmaster" / "Wayfarer"
(Ray-Ban trademarks) must no longer appear as our style names.
"""
import importlib.util
import json
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import brand_compliance as bc  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


RISKY_NAME = re.compile(
    r"BOSS|DIESEL|POLO|TOMMY|NORTH FACE|BRABUS|LOUIS|STAR WARS|PRADA|PUMA|RAY-?BAN|OAKLEY|"
    r"CARTIER|UNITED COLORS|\bGAS\b|TOM HARDY|SHARAPOVA|JACK JOHNSON|CADILL|DASAULTS|"
    r"QUEEN ELIZABETH|SHANGRI|BANFF|IDEE|FENNI|EYE\+\+|D & T|\bRB\b|CLUBMASTER|WAYFARER", re.I)


class HoldListTests(unittest.TestCase):
    def test_owner_kept_frames_are_not_held_or_renamed(self):
        for pid in (1, 362):          # BB44 Optiwar baby frame, AK98 Snoopy print
            self.assertNotIn(pid, bc.HELD)
            self.assertNotIn(pid, bc.RENAME)

    def test_confirmed_logo_frames_are_held(self):
        for pid, sku in ((668, "AB27"), (665, "AB29"), (694, "AB71"), (550, "BH90"),
                         (444, "AL61"), (332, "AL69"), (675, "AD99")):
            self.assertEqual(bc.HELD[pid][0], sku)

    def test_every_hold_says_why(self):
        for pid, (sku, kind, reason) in bc.HELD.items():
            self.assertIn(kind, ("frame_mark", "lens_sticker"), sku)
            self.assertTrue(reason.strip(), sku)

    def test_new_names_carry_no_brand(self):
        for pid, (sku, old, new) in bc.RENAME.items():
            self.assertTrue(new.startswith("OPTIWAR "), new)
            self.assertTrue(new.endswith(sku), new)
            self.assertIsNone(RISKY_NAME.search(new), new)

    def test_frames_with_a_mark_on_the_frame_are_not_renamed_for_relisting(self):
        for pid, (_sku, kind, _r) in bc.HELD.items():
            if kind == "frame_mark":
                self.assertNotIn(pid, bc.RENAME)

    def test_is_held_accepts_rows_and_ids(self):
        self.assertTrue(bc.is_held(665))
        self.assertTrue(bc.is_held({"product_id": "665"}))
        self.assertFalse(bc.is_held({"product_id": 1}))
        self.assertFalse(bc.is_held(None))

    def test_sql_exclude_lists_every_held_id(self):
        clause = bc.sql_exclude("p")
        self.assertTrue(clause.startswith(" AND p.product_id NOT IN ("))
        ids = {int(x) for x in re.findall(r"\d+", clause)}
        self.assertEqual(ids, set(bc.HELD))


class CatalogueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = _load("catalogue_under_test", os.path.join(REPO, "catalogue.py"))

    def test_held_frame_is_refused_on_both_sites_whatever_the_columns_say(self):
        row = {"product_id": 665, "product_vertical": "EYEWEAR", "sell_on_in": 1, "sell_on_com": 1}
        self.assertFalse(self.cat.is_product_allowed(row, site=self.cat.SITE_IN))
        self.assertFalse(self.cat.is_product_allowed(row, site=self.cat.SITE_COM))

    def test_kept_frame_is_still_allowed(self):
        row = {"product_id": 1, "product_vertical": "EYEWEAR", "sell_on_in": 1, "sell_on_com": 1}
        self.assertTrue(self.cat.is_product_allowed(row, site=self.cat.SITE_IN))
        self.assertTrue(self.cat.is_product_allowed(row, site=self.cat.SITE_COM))

    def test_every_listing_filter_excludes_held_frames(self):
        for alias in (None, "p"):
            clause = self.cat.catalogue_site_filter(site=self.cat.SITE_IN, alias=alias)
            self.assertIn(bc.sql_exclude(alias), clause)


class RetiredStyleNameTests(unittest.TestCase):
    PAGES = ("templates/base.html", "templates/index.html", "templates/categories.html",
             "templates/all_frames.html", "templates/guide_frame_shapes.html")

    def test_pages_no_longer_call_a_style_clubmaster_or_wayfarer(self):
        for page in self.PAGES:
            text = _read(page).replace("Genuine Ray-Ban Meta Wayfarer Gen 2", "")
            # column names (product_category_clubmaster) are internal, not shown
            shown = re.sub(r"product_category_\w+", "", text)
            # old links (?shape=clubmaster) are translated, never shown
            shown = shown.replace("{clubmaster: 'browline', wayfarer: 'classicsquare'}", "")
            hits = re.findall(r"(?i).{0,40}(?:club ?master|wayfarer).{0,40}", shown)
            self.assertEqual(hits, [], page)

    def test_old_category_url_moves_permanently_to_its_nearest_category(self):
        models = _read("models.py")
        self.assertIn("RETIRED_CATEGORIES = {'clubmaster': 'browline', 'wayfarer': 'square'}", models)
        self.assertRegex(models, r"moved = RETIRED_CATEGORIES\.get\(category\)\s+if moved:\s+"
                                 r"return redirect\(url_for\('main\.categories', category=moved\), code=301\)")
        self.assertIn("'browline': 'SELECT * FROM products WHERE (product_category_clubmaster = 1 "
                      "OR product_category_browline = 1)", models)

    def test_product_page_style_label_uses_generic_names(self):
        models = _read("models.py")
        self.assertIn("('product_category_clubmaster', 'Browline'),", models)
        self.assertIn("('product_category_wayfarer', 'Classic Square'),", models)

    def test_ai_text_uses_generic_names(self):
        for rel in ("static/ai/frame_rules.json", "static/ai/optiwar_ai_knowledge_base.json"):
            self.assertNotRegex(_read(rel), r"(?i)clubmaster", rel)
        self.assertIn("browline/classicsquare", _read("chat_gateway.py"))


class ChatCatalogueTests(unittest.TestCase):
    def test_chat_catalogue_has_no_held_frame_and_no_risky_name(self):
        data = json.loads(_read("static/ai/product_catalog.json"))
        for p in data["products"]:
            self.assertNotIn(int(p["id"]), bc.HELD, p["name"])
            self.assertIsNone(RISKY_NAME.search(p["name"]), p["name"])
            for s in p.get("shapes") or []:
                self.assertNotIn(s, ("clubmaster", "wayfarer"), p["name"])
        self.assertEqual(data["total_products"], len(data["products"]))

    def test_stale_static_seo_copy_is_gone(self):
        self.assertFalse(os.path.exists(os.path.join(REPO, "static", "seo")))


class ApplyScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = _load("brand_apply_under_test",
                           os.path.join(REPO, "scripts", "brand_compliance_apply.py"))

    def test_plan_switches_held_frames_off_and_renames(self):
        held_pid = 665
        ren_pid, (sku, old, new) = next(iter(sorted(bc.RENAME.items())))
        rows = {
            held_pid: {"product_id": held_pid, "product_name": "BOSS AB29", "product_slug": "boss",
                       "product_details": "BOSS AB29 - Black frame.", "product_image": "",
                       "sell_on_in": 1, "sell_on_com": 1},
            ren_pid: {"product_id": ren_pid, "product_name": old, "product_slug": "old",
                      "product_details": "%s - frame." % old, "product_image": "",
                      "sell_on_in": 1, "sell_on_com": 1},
        }
        changes = dict(self.script.plan(rows))
        self.assertEqual(changes[held_pid], {"sell_on_in": (1, 0), "sell_on_com": (1, 0)})
        self.assertEqual(changes[ren_pid]["product_name"], (old, new))
        self.assertEqual(changes[ren_pid]["product_slug"], ("old", self.script.slugify(new)))
        self.assertEqual(changes[ren_pid]["product_details"][1], "%s - frame." % new)

    def test_plan_is_empty_once_applied(self):
        rows = {665: {"product_id": 665, "product_name": "BOSS AB29", "product_slug": "boss",
                      "product_details": "", "product_image": "", "sell_on_in": 0, "sell_on_com": 0}}
        self.assertEqual(self.script.plan(rows), [])

    def test_image_paths_accept_both_stored_forms(self):
        row = {"product_image": "/static/catalog/opticalbazaar/AB29/AB29_1.jpg, catalog/x/AB29_2.jpg,NULL"}
        self.assertEqual(self.script.image_paths(row),
                         ["catalog/opticalbazaar/AB29/AB29_1.jpg", "catalog/x/AB29_2.jpg"])


if __name__ == "__main__":
    unittest.main()
