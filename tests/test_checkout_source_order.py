"""Names the checkout handlers use must exist before they are used.

On 7 Nov 2026 a block added to ``checkout()`` read ``cursor`` twenty-seven
lines before the function created it: every POST raised UnboundLocalError and
no test noticed, because none posts a checkout past the terms. This test reads
the source: inside each handler, the first read of a local that the function
also assigns must come after its first assignment, on a path that cannot skip
it (top-level statements of the function body only, which is where both were).
"""
import ast
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HANDLERS = ("checkout", "checkout_page", "success")
WATCHED = ("cursor", "db", "grand_total", "customer_state", "customer_postcode", "customer_address")


def _first_lines(fn, name):
    stores, loads = [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == name:
            (stores if isinstance(node.ctx, ast.Store) else loads).append(node.lineno)
    return (min(stores) if stores else None), (min(loads) if loads else None)


class CheckoutSourceOrder(unittest.TestCase):

    def test_locals_are_assigned_before_they_are_read(self):
        with open(os.path.join(REPO, "models.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        seen = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or fn.name not in HANDLERS:
                continue
            seen.add(fn.name)
            for name in WATCHED:
                store, load = _first_lines(fn, name)
                if store is None or load is None:
                    continue
                self.assertLessEqual(
                    store, load, "%s(): %r is read on line %d before it is assigned on line %d"
                    % (fn.name, name, load, store))
        self.assertIn("checkout", seen)

    def test_the_delivery_block_sits_below_the_cursor(self):
        with open(os.path.join(REPO, "models.py"), encoding="utf-8") as fh:
            source = fh.read()
        fn = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == "checkout")
        body = ast.get_source_segment(source, fn)
        self.assertLess(body.index("cursor = db.cursor()"), body.index("shipping_options.choose("))
        self.assertLess(body.index("shipping_options.choose("), body.index("total_amount = grand_total"))


if __name__ == "__main__":
    unittest.main()
