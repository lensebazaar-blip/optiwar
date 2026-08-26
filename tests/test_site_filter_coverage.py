"""Every products query either applies site eligibility or says why it need not.

The eligibility rule is one predicate, and its weakness is arithmetic: it holds
only while *all* of the read paths apply it. A leak is therefore not a bug in
``catalogue.py`` but a query somebody added later — a new listing, a new API, a
new recommendation endpoint — and no runtime test of the surfaces that exist
today can catch that.

So this reads the source: any function that queries ``products`` must apply the
predicate, or check the row with ``is_product_allowed``/``sellable_here``, or
appear below with a reason it is exempt. A new query is a failing test until
somebody has decided which of the three it is.

    python3 -m unittest tests.test_site_filter_coverage
"""
import ast
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Modules that serve a storefront. Admin/back-office tools (dashboard_admin.py,
# the one-off price scripts, stock_writeoff.py) are deliberately absent: staff
# operate on the whole catalogue regardless of which host they logged in from,
# and hiding a lens from an order-team screen would be a defect, not a fix.
STOREFRONT = ("models.py", "chat.py", "ai_api.py", "orders.py")

APPLIES = ("catalogue_site_filter", "is_product_allowed", "sellable_here")

# Reasons a storefront query does not need the predicate. Frame-only queries are
# the common one: they already restrict to a frame category or a frame-shape
# column, so no lens can be in the result on any host. Kept as a stated reason
# per function rather than a name pattern, so a query that stops being
# frame-only fails here.
EXEMPT = {
    "all_spectacle_frames":
        "excludes Contact Lenses and Hearing Aids by category",
    "api_v1_categories":
        "counts frame-shape columns only",
    "guide_frame_shapes":
        "counts frame-shape columns only",
    "api_tryon_frames":
        "product_category = 'Spectacles Frame'",
    "api_tryon_matching_frames":
        "product_category = 'Spectacles Frame'",
    "checkout_page":
        "prices and images for a cart already admitted by the add-to-cart guard",
    "checkout":
        "images for a cart already admitted by the add-to-cart guard",
    "success":
        "reads back what was ordered; an order is history, not a listing",
}

FRAME_ONLY = (
    "product_category = 'Spectacles Frame'",
    'product_category = "Spectacles Frame"',
    "NOT IN ('Contact Lenses'",
)

PRODUCTS_QUERY = re.compile(r"\bfrom\s+products\b", re.IGNORECASE)

SELECT_ALL = re.compile(r"\bselect\s+\*", re.IGNORECASE)

# Surfaces that reach one product by id, code or slug, where a WHERE clause
# cannot decide anything: the caller already knows which product it wants. These
# are the routes a .in visitor would use to reach a lens directly — the URL, the
# APIs behind the URL, and the two cart posts — so each must check the row.
SINGLE_ROW_GUARDED = {
    "models.py": ("product_page", "api_product_detail",
                  "api_v1_product_detail", "api_v1_product_media",
                  "add_to_cart", "add_to_cart_wcl"),
}

ROW_CHECKS = ("is_product_allowed", "sellable_here")


def _functions(path):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, ast.get_source_segment(src, node) or ""


class SiteFilterCoverageTest(unittest.TestCase):
    def test_every_storefront_products_query_is_accounted_for(self):
        unguarded = []
        for module in STOREFRONT:
            for name, body in _functions(os.path.join(REPO, module)):
                if not PRODUCTS_QUERY.search(body):
                    continue
                if any(token in body for token in APPLIES):
                    continue
                if name in EXEMPT:
                    continue
                unguarded.append("%s:%s" % (module, name))
        self.assertEqual(
            unguarded, [],
            "these query products without applying site eligibility; add the "
            "predicate, check the row, or record the reason in EXEMPT: %s"
            % unguarded)

    def test_the_exemptions_still_describe_real_functions(self):
        # An exemption for a function that no longer exists is a licence nobody
        # is using, and the next function of that name inherits it silently.
        names = set()
        for module in STOREFRONT:
            names.update(n for n, _b in _functions(os.path.join(REPO, module)))
        self.assertEqual(sorted(set(EXEMPT) - names), [])

    def test_the_frame_only_exemptions_are_still_frame_only(self):
        bodies = {}
        for module in STOREFRONT:
            for name, body in _functions(os.path.join(REPO, module)):
                bodies[name] = body
        for name, reason in EXEMPT.items():
            if "frame" not in reason.lower():
                continue
            body = bodies[name]
            self.assertTrue(
                any(marker in body for marker in FRAME_ONLY)
                or "_category_" in body,
                "%s is exempt as frame-only but no longer restricts to frames"
                % name)

    def test_the_direct_access_surfaces_check_the_row_they_fetched(self):
        # A listing predicate is no defence here: /categories/contact-lenses/x
        # and /api/products/1005 name the product, so only a check on the
        # fetched row can answer "not on this storefront".
        missing = []
        for module, functions in SINGLE_ROW_GUARDED.items():
            bodies = dict(_functions(os.path.join(REPO, module)))
            for name in functions:
                body = bodies.get(name)
                self.assertIsNotNone(body, "%s:%s no longer exists"
                                     % (module, name))
                if not any(check in body for check in ROW_CHECKS):
                    missing.append("%s:%s" % (module, name))
        self.assertEqual(
            missing, [],
            "these reach one product directly and no longer check whether this "
            "storefront sells it: %s" % missing)

    def test_a_row_check_is_given_the_columns_it_decides_on(self):
        # The quiet failure mode: is_contact_lens() / is_product_allowed() read
        # the row dict, so a projection that omits product_vertical or the
        # sell_on_* flags makes them fall back to EYEWEAR and answer "allowed"
        # for everything. The guard still reads as if it works.
        needed = {"is_contact_lens": ("product_vertical",),
                  "is_product_allowed": ("product_vertical", "sell_on_com")}
        starved = []
        for module in STOREFRONT:
            for name, body in _functions(os.path.join(REPO, module)):
                if not PRODUCTS_QUERY.search(body) or SELECT_ALL.search(body):
                    continue
                for check, columns in needed.items():
                    if check + "(" not in body:
                        continue
                    if not any(col in body for col in columns):
                        starved.append("%s:%s -> %s" % (module, name, check))
        self.assertEqual(
            starved, [],
            "these hand a row to a check whose deciding column the SELECT does "
            "not fetch, so the check silently passes everything: %s" % starved)

    def test_no_module_writes_its_own_version_of_the_predicate(self):
        # Selecting the columns is fine — the media API does, to hand the row to
        # is_product_allowed. Comparing them in SQL is not: that is the rule
        # re-implemented, and a second implementation is one that can disagree.
        own = re.compile(r"sell_on_(com|in)\s*(=|<>|!=|IS)", re.IGNORECASE)
        for module in STOREFRONT:
            with open(os.path.join(REPO, module), encoding="utf-8") as fh:
                self.assertEqual(own.findall(fh.read()), [], module)


if __name__ == "__main__":
    unittest.main()
