"""The two decisions in the write-off tool that are easy to get wrong.

Re-running it must not overwrite the reason an earlier write-off — or a real
sale — recorded, and the undo script must restore the state that existed
*before* the run rather than the state after it.
"""
import importlib.util
import os
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "stock_writeoff", os.path.join(REPO, "scripts", "stock_writeoff.py"))
wo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wo)


def row(pid, code, qty, status):
    return dict(product_id=pid, product_code=code, product_quantity=qty,
                product_status=status)


class NeedsChangeTests(unittest.TestCase):
    def test_stock_on_hand_is_always_written_off(self):
        self.assertTrue(wo.needs_change(row(1, "A", 3, "ACTIVE"), "OUT_OF_STOCK"))
        self.assertTrue(wo.needs_change(row(1, "A", 1, "SEASONAL"), "OUT_OF_STOCK"))

    def test_a_product_already_sold_out_is_left_untouched(self):
        # 786 went to zero through a paid order, carrying "auto: stock depleted".
        # Writing off the box must not rewrite that as a loss.
        self.assertFalse(wo.needs_change(row(786, "AH02", 0, "OUT_OF_STOCK"),
                                         "OUT_OF_STOCK"))

    def test_zero_stock_still_moves_when_the_target_state_differs(self):
        self.assertTrue(wo.needs_change(row(1, "A", 0, "OUT_OF_STOCK"),
                                        "DISCONTINUED"))

    def test_a_deliberate_lifecycle_decision_is_never_overridden(self):
        # SEASONAL/DISCONTINUED/ARCHIVED are commercial decisions; losing the
        # stock does not change them, so an already-empty one needs nothing.
        for state in ("SEASONAL", "DISCONTINUED", "ARCHIVED"):
            self.assertFalse(wo.needs_change(row(1, "A", 0, state), "OUT_OF_STOCK"),
                             state)

    def test_a_null_quantity_counts_as_empty(self):
        self.assertFalse(wo.needs_change(row(1, "A", None, "OUT_OF_STOCK"),
                                         "OUT_OF_STOCK"))


class RestoreScriptTests(unittest.TestCase):
    def test_the_undo_script_carries_the_prior_quantity_and_status(self):
        rows = [row(784, "AH08", 1, "ACTIVE"), row(789, "AH05", 1, "SEASONAL")]
        path = os.path.join(tempfile.mkdtemp(), "undo.sql")
        wo.restore_script(rows, path)
        with open(path) as fh:
            sql = fh.read()
        self.assertIn("product_quantity=1", sql)
        self.assertIn("product_status='ACTIVE'", sql)
        self.assertIn("WHERE product_id=784", sql)
        self.assertIn("product_status='SEASONAL'", sql)
        self.assertIn("WHERE product_id=789", sql)
        self.assertIn("START TRANSACTION;", sql)
        self.assertIn("COMMIT;", sql)


if __name__ == "__main__":
    unittest.main()
