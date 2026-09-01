"""What the assistant may say about contact lenses, and where it learns it.

The defect this locks down was a single sentence in the system prompt — "We DO
NOT sell contact lenses on optiwar.com" — recited to .com customers as fact. It
was true when written, so no test caught it; it becomes a lie the moment a lens
is loaded, and the model cannot tell. The same claim also existed as a knowledge
base FAQ saying the opposite ("Yes, Optiwar offers contact lenses"), so the two
sources contradicted each other inside one prompt.

The rule asserted here: nothing in a prompt states whether a vertical is for
sale. The storefront's eligibility and the release gate answer that, per request,
and the prompt carries only policy (what to do when there is nothing to offer)
and facts read from rows.

    python3 -m unittest tests.test_lens_prompt_from_catalogue
"""
import importlib.util
import json
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, filename=None):
    spec = importlib.util.spec_from_file_location(
        name + "_under_test", os.path.join(REPO, filename or (name + ".py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lens_prompt = _load("lens_prompt")
catalogue = _load("catalogue")

# A lens with everything the release gate asks for. Individual tests remove one
# field at a time, so the fixture is the only place the "complete" shape lives.
LIVE = {
    "product_id": 2001,
    "product_code": "CL-AC-DT1",
    "product_name": "Dailies Total 1",
    "product_slug": "dailies-total-1-30",
    "product_image": "dailies_total_1.jpg",
    "product_status": "ACTIVE",
    "product_vertical": "CONTACT_LENS",
    "sell_on_com": 1,
    "sell_on_in": 0,
    "product_price_eur": 45.00,
    "product_special_price_eur": 39.90,
    "brand": "Alcon",
    "manufacturer": "Alcon",
    "gtin": "3664798000023",
    "manufacturer_mpn": "DT1-30",
    "modality": "DAILY",
    "lens_type": "SPHERICAL",
    "pack_quantity": 30,
    "material": "delefilcon A",
    "availability": "IN_STOCK",
    "merchant_enabled": 1,
    "replacement_days": 1,
    "variant_count": 61,
    "image_count": 2,
}

MATRIX = {
    "variants": 61,
    "sph_min": -12.00, "sph_max": 6.00,
    "cyl_min": None, "cyl_max": None,
    "axis_min": None, "axis_max": None,
    "add_min": None, "add_max": None,
    "bc_min": 8.5, "bc_max": 8.5,
    "dia_min": 14.1, "dia_max": 14.1,
    "colors": [],
}


class FakeCursor(object):
    """Answers every query with the rows given, and remembers the SQL.

    Deliberately ignores the WHERE clause: a row-level check that only works
    because the query filtered is untested, and the .in invariant has to hold
    even when a projection or a predicate is wrong.
    """

    def __init__(self, rows=(), matrix=None, colors=()):
        self.rows, self.matrix, self.colors = list(rows), matrix or {}, list(colors)
        self.sql = []
        self._last = None

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if "FROM contact_lens_products" in sql:
            self._last = [dict(r) for r in self.rows]
        elif "FROM contact_lens_variants" in sql and "COUNT(*)" in sql:
            self._last = [dict(self.matrix)]
        elif "DISTINCT color_code" in sql:
            self._last = [{"color_code": c, "color_name": n} for c, n in self.colors]
        else:
            self._last = [dict(r) for r in self.rows]

    def fetchall(self):
        return self._last or []

    def fetchone(self):
        return (self._last or [None])[0]


class ReleaseGateTests(unittest.TestCase):
    def test_a_complete_lens_is_live_on_com(self):
        self.assertEqual(
            catalogue.lens_release_blockers(LIVE, catalogue.SITE_COM), ())
        self.assertTrue(catalogue.is_lens_live(LIVE, catalogue.SITE_COM))

    def test_the_same_lens_is_not_live_on_in(self):
        # The whole point of the flag: India activation is an eligibility
        # change, not a code change, and until it happens the row is invisible
        # even though it is complete.
        blockers = catalogue.lens_release_blockers(LIVE, catalogue.SITE_IN)
        self.assertIn("not sold on in.optiwar.com", blockers)

    def test_a_row_in_the_database_is_not_a_released_product(self):
        # ~70 products get loaded before four are released; a DB row must not be
        # enough to reach a customer.
        draft = dict(LIVE, merchant_enabled=0)
        self.assertIn("merchant_enabled=0",
                      catalogue.lens_release_blockers(draft, catalogue.SITE_COM))

    def test_each_missing_release_requirement_is_named(self):
        cases = {
            "no landing page": dict(product_slug=""),
            "no primary image": dict(product_image="", image_count=0),
            "no EUR price": dict(product_price_eur=0,
                                 product_special_price_eur=None),
            "no availability": dict(availability=""),
            "no brand": dict(brand=""),
            "no prescription matrix": dict(variant_count=0),
        }
        for reason, change in cases.items():
            row = dict(LIVE, **change)
            self.assertIn(
                reason,
                catalogue.lens_release_blockers(row, catalogue.SITE_COM),
                "%s did not block release" % reason)

    def test_a_missing_identifier_does_not_hold_a_lens_back(self):
        # No supplier holds a GTIN or a manufacturer part number for these
        # lenses. Blocking release on it would either stop the pilot or invite
        # somebody to type a code in; the feed declares identifier_exists=false.
        row = dict(LIVE, gtin=None, manufacturer_mpn="")
        self.assertEqual(
            catalogue.lens_release_blockers(row, catalogue.SITE_COM), ())

    def test_a_lens_stated_as_rules_is_live_on_its_stated_values(self):
        # A RULES lens has no combination rows and is not half-loaded for it:
        # the stated values are what make it orderable, and counting only
        # combinations would hold every such lens back forever.
        stated = dict(LIVE, param_mode="RULES", variant_count=0, rule_count=75)
        self.assertEqual(
            catalogue.lens_release_blockers(stated, catalogue.SITE_COM), ())
        nothing = dict(stated, rule_count=0)
        self.assertIn("no selectable values stated",
                      catalogue.lens_release_blockers(nothing,
                                                      catalogue.SITE_COM))

    def test_a_discontinued_lens_is_not_live(self):
        row = dict(LIVE, product_status="DISCONTINUED")
        self.assertTrue(any(b.startswith("status")
                            for b in catalogue.lens_release_blockers(
                                row, catalogue.SITE_COM)))

    def test_the_query_carries_the_storefront_predicate(self):
        cur = FakeCursor([LIVE], MATRIX)
        catalogue.lens_rows(cur, catalogue.SITE_IN)
        self.assertIn("p.sell_on_in = 1", cur.sql[0])

    def test_in_gets_no_lens_even_from_a_query_that_returned_one(self):
        cur = FakeCursor([LIVE], MATRIX)
        self.assertEqual(catalogue.live_lenses(cur, catalogue.SITE_IN), [])
        self.assertEqual(
            [r["product_code"] for r in catalogue.live_lenses(
                cur, catalogue.SITE_COM)], ["CL-AC-DT1"])

    def test_matrix_summary_reads_ranges_and_colours(self):
        cur = FakeCursor([LIVE], MATRIX, colors=[("AZ", "Azure Blue")])
        summary = catalogue.lens_matrix_summary(cur, 2001)
        self.assertEqual(summary["sph_min"], -12.00)
        self.assertEqual(summary["colors"], [("AZ", "Azure Blue")])


class PromptTextTests(unittest.TestCase):
    def test_in_says_not_available_here_and_names_nothing(self):
        text = lens_prompt.contact_lens_section((), is_india=True)
        self.assertIn("not currently available on this store", text)
        self.assertNotIn("Alcon", text)
        # It may mention the global store, but only when asked about it.
        self.assertIn("explicitly ask", text)

    def test_com_with_nothing_released_offers_nothing_and_promises_nothing(self):
        text = lens_prompt.contact_lens_section([])
        self.assertIn("No contact lens is released for sale", text)
        self.assertNotIn("Alcon", text)
        self.assertIn("Do not promise a date", text)

    def test_com_states_the_facts_the_catalogue_holds(self):
        text = lens_prompt.contact_lens_section([(LIVE, MATRIX)])
        for fact in ("Alcon", "Dailies Total 1", "daily spherical", "pack of 30",
                     "\u20ac39.90 per pack", "IN_STOCK", "SPH -12.00 to +6.00",
                     "BC 8.5", "DIA 14.1"):
            self.assertIn(fact, text, "%r missing from the lens section" % fact)
        # A range is not a promise that every step inside it exists.
        self.assertIn("not a guarantee that every combination", text)
        self.assertIn("Do not offer a nearest or corrected power", text)

    def test_a_toric_multifocal_reads_as_words_not_as_a_column_value(self):
        row = dict(LIVE, lens_type="TORIC_MULTIFOCAL", modality="MONTHLY")
        matrix = dict(MATRIX, cyl_min=-1.75, cyl_max=-0.75,
                      axis_min=10, axis_max=180, add_min=1.5, add_max=2.5)
        text = lens_prompt.contact_lens_section([(row, matrix)])
        self.assertIn("monthly toric multifocal", text)
        self.assertIn("CYL -1.75 to -0.75", text)
        self.assertIn("AXIS 10 to 180", text)
        self.assertIn("ADD +1.50 to +2.50", text)

    def test_an_unreadable_catalogue_is_not_a_claim(self):
        # The gateway returns SECTION_NONE when the query raises: an outage must
        # not become "we don't sell contact lenses".
        self.assertIn("No contact lens is released",
                      lens_prompt.SECTION_NONE)
        self.assertNotIn("do not sell", lens_prompt.SECTION_NONE.lower())


class NoAvailabilityProseTests(unittest.TestCase):
    """The general rule, not the one sentence that was wrong."""

    CLAIM = re.compile(
        r"(do(es)? ?n[o']?t sell contact|we sell contact|"
        r"offers? contact lenses|carries most range)", re.IGNORECASE)

    def _prompt_source(self, module):
        with open(os.path.join(REPO, module), encoding="utf-8") as fh:
            return fh.read()

    def test_no_prompt_asserts_whether_we_sell_the_vertical(self):
        for module in ("chat_gateway.py", "chat.py", "lens_prompt.py"):
            src = self._prompt_source(module)
            # Comments explain the defect by naming it; the claim must not
            # survive in code or in prompt text.
            code = "\n".join(ln for ln in src.splitlines()
                             if not ln.lstrip().startswith("#"))
            found = self.CLAIM.findall(code)
            self.assertEqual(found, [],
                             "%s still states catalogue availability: %s"
                             % (module, found))

    def test_both_prompts_include_the_catalogue_built_section(self):
        # Building the section is not enough — it has to reach the prompt, so
        # assert the interpolation as well as the call.
        for module, call, slot in (
                ("chat_gateway.py", "_build_contact_lens_section",
                 "{contact_lens_section}"),
                ("chat.py", "_build_contact_lens_prompt",
                 "{contact_lens_text}")):
            src = self._prompt_source(module)
            self.assertIn(call + "(", src)
            self.assertIn(slot, src, "%s builds the section but never uses it"
                          % module)

    def test_the_knowledge_base_no_longer_answers_the_question(self):
        path = os.path.join(REPO, "static", "ai",
                            "optiwar_ai_knowledge_base.json")
        with open(path, encoding="utf-8") as fh:
            kb = json.load(fh)
        kept = [item for item in kb.get("faq", [])
                if not lens_prompt.is_lens_availability_faq(item)]
        surviving = [i.get("question") for i in kept
                     if self.CLAIM.search(json.dumps(i))]
        self.assertEqual(surviving, [],
                         "FAQ still tells the model what we stock: %s"
                         % surviving)
        # And the filter is not a blanket one: frames survive it.
        self.assertTrue(any("frame" in json.dumps(i).lower() for i in kept))


if __name__ == "__main__":
    unittest.main()
