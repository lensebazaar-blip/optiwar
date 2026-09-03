"""An owner's preview of an unreleased lens: one product, one secret, a clock."""
import importlib.util
import os
import unittest

from jinja2 import Environment

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "lens_preview_under_test", os.path.join(REPO, "lens_preview.py"))
lens_preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lens_preview)

KEY = "a-real-secret"
NOW = 1_800_000_000


class SecretTest(unittest.TestCase):

    def test_the_development_default_never_signs_a_preview(self):
        self.assertIsNone(lens_preview.secret({}))
        self.assertIsNone(lens_preview.secret(
            {"SECRET_KEY": lens_preview._DEFAULT_SECRET_KEY}))
        self.assertEqual(lens_preview.secret({"SECRET_KEY": "x"}), "x")
        self.assertEqual(lens_preview.secret(
            {"SECRET_KEY": "x", "LENS_PREVIEW_SECRET": "y"}), "y")


class TokenTest(unittest.TestCase):

    def test_a_genuine_unexpired_token_opens_its_own_product_only(self):
        token = lens_preview.issue(KEY, 1015, 72, now=NOW)
        self.assertEqual(lens_preview.verify(KEY, 1015, token, now=NOW),
                         NOW + 72 * 3600)
        self.assertEqual(lens_preview.verify(KEY, "1015", token, now=NOW),
                         NOW + 72 * 3600)
        self.assertIsNone(lens_preview.verify(KEY, 1016, token, now=NOW))

    def test_expired_tampered_or_foreign_tokens_open_nothing(self):
        token = lens_preview.issue(KEY, 1015, 1, now=NOW)
        self.assertIsNone(lens_preview.verify(KEY, 1015, token,
                                              now=NOW + 3600))
        expires, _, sig = token.partition(".")
        self.assertIsNone(lens_preview.verify(
            KEY, 1015, "%s.%s" % (expires, sig[:-1] + "0"), now=NOW))
        self.assertIsNone(lens_preview.verify(
            KEY, 1015, "%d.%s" % (int(expires) + 99999, sig), now=NOW))
        self.assertIsNone(lens_preview.verify("other", 1015, token, now=NOW))
        self.assertIsNone(lens_preview.verify(None, 1015, token, now=NOW))
        for bad in ("", None, "abc", "123", ".sig", "x.y", token + "z"):
            self.assertIsNone(lens_preview.verify(KEY, 1015, bad, now=NOW))

    def test_a_link_cannot_be_issued_without_a_secret_or_for_ever(self):
        with self.assertRaises(ValueError):
            lens_preview.issue(None, 1015, 1)
        with self.assertRaises(ValueError):
            lens_preview.issue(KEY, 1015, 0)
        with self.assertRaises(ValueError):
            lens_preview.issue(KEY, 1015, lens_preview.MAX_HOURS + 1)


class SessionTest(unittest.TestCase):

    def test_a_grant_is_per_product_and_expires(self):
        session = {}
        lens_preview.grant(session, 1015, NOW + 10)
        self.assertTrue(lens_preview.granted(session, 1015, now=NOW))
        self.assertTrue(lens_preview.granted(session, "1015", now=NOW))
        self.assertFalse(lens_preview.granted(session, 1016, now=NOW))
        self.assertFalse(lens_preview.granted(session, 1015, now=NOW + 10))
        self.assertFalse(lens_preview.granted({}, 1015, now=NOW))
        self.assertFalse(lens_preview.granted(
            {"lens_preview": {"1015": "soon"}}, 1015, now=NOW))


class PreviewableTest(unittest.TestCase):

    def test_only_the_release_flag_may_stand_between_owner_and_page(self):
        self.assertTrue(lens_preview.previewable(
            {"release_blockers": ["merchant_enabled=0"]}))
        self.assertFalse(lens_preview.previewable({"release_blockers": []}))
        self.assertFalse(lens_preview.previewable(
            {"release_blockers": ["merchant_enabled=0", "no images"]}))
        self.assertFalse(lens_preview.previewable(
            {"release_blockers": ["not sold on optiwar.in",
                                  "merchant_enabled=0"]}))


class RoutesTest(unittest.TestCase):
    """The storefront's wiring, read from the source it will run."""

    def setUp(self):
        with open(os.path.join(REPO, "models.py")) as fh:
            self.models = fh.read()

    def test_every_lens_surface_goes_through_the_same_gate(self):
        for route in ("def lens_select", "def lens_add_to_cart"):
            body = self.models[self.models.index(route):]
            body = body[:body.index("\n@bp.route")]
            self.assertIn("_released_or_previewed_lens(", body, route)
            self.assertNotIn("_released_lens(cursor", body, route)
        page = self.models[self.models.index("def product_page"):]
        page = page[:page.index("\n@bp.route")]
        self.assertIn("lens_preview.previewable(lens)", page)
        self.assertIn("_noindex(make_response(page))", page)

    def test_a_preview_shows_the_markup_the_release_would_publish(self):
        """The owner is approving the JSON-LD too, so the row is read with its
        one blocker lifted; and a lens never falls back to the frame markup."""
        page = self.models[self.models.index("def product_page"):]
        page = page[:page.index("\n@bp.route")]
        self.assertIn("dict(lens, release_blockers=[]) if lens_previewing",
                      page)
        with open(os.path.join(REPO, "templates",
                               "product_page.html")) as fh:
            tpl = fh.read()
        head = tpl.index("{% if lens_jsonld %}")
        self.assertIn("{% elif not lens %}", tpl[head:head + 200])

    def test_a_lens_row_with_no_frame_stock_count_still_renders(self):
        """A lens has no product_quantity; the frame stock badge, which
        compares it with an int, must not be reached for a lens."""
        with open(os.path.join(REPO, "templates",
                               "product_page.html")) as fh:
            tpl = fh.read()
        start = tpl.index("<!-- Stock -->")
        block = tpl[start:tpl.index("<!-- Frame Specs -->", start)]
        env = Environment()
        env.globals["_"] = lambda s: s
        html = env.from_string(block).render(
            product={"product_quantity": None}, lens={"availability": "IN_STOCK"})
        self.assertNotIn("pdp-stock", html)
        frame = env.from_string(block).render(
            product={"product_quantity": 9}, lens=None)
        self.assertIn("In Stock", frame)

    def test_the_preview_never_opens_on_optiwar_in(self):
        gate = self.models[self.models.index("def _lens_preview_open"):]
        gate = gate[:gate.index("\ndef ")]
        self.assertIn("current_site() == SITE_IN", gate)
        self.assertIn("lens_preview.secret(os.environ)", gate)


if __name__ == "__main__":
    unittest.main()
