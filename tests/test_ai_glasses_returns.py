"""AI Glasses returns: dead on arrival only (policy clause 13A, the checkout
notice and the My Orders return request), and refunded orders leave the sold count."""
import importlib
import importlib.util
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tests import test_reverse_pickup as trp  # noqa: E402

rp = trp.rp
rreq = importlib.import_module(trp.PKG + ".return_request")


def _load(name):
    spec = importlib.util.spec_from_file_location(name + "_aig_returns", os.path.join(REPO, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pt = _load("policy_terms")
aig = _load("ai_glasses")

AIG = {"ai_glasses": 1, "product_name": "Ray-Ban Meta Wayfarer Gen 2", "order_quantity": 1,
       "lens_price": 9900, "server_total_price": 39825, "aig_lens_label": "Progressive"}
FRAME = {"product_name": "Frame 69", "order_quantity": 1, "ATC_total": 699, "server_total_price": 1000}


class _Flag:
    def __init__(self, on):
        self.on = on

    def __enter__(self):
        self.saved = os.environ.pop(pt.AIG_FLAG, None)
        if self.on:
            os.environ[pt.AIG_FLAG] = "1"

    def __exit__(self, *_a):
        os.environ.pop(pt.AIG_FLAG, None)
        if self.saved is not None:
            os.environ[pt.AIG_FLAG] = self.saved


class PolicyTest(unittest.TestCase):
    def test_flag_off_keeps_the_sealed_india_version(self):
        with _Flag(False):
            cur = pt.current("optiwar.in")
            self.assertEqual(cur["returns"]["version"], "2026-10-03-in")
            self.assertNotIn("13A. AI GLASSES", cur["returns"]["text"])
            self.assertNotIn("AI Glasses", pt.returns_html("optiwar.in"))

    def test_flag_on_is_a_new_india_version_with_the_doa_clause(self):
        with _Flag(True):
            cur = pt.current("optiwar.in")
            self.assertEqual(cur["returns"]["version"], "2026-10-31-in")
            text = cur["returns"]["text"]
            self.assertLess(text.index("13A. AI GLASSES"), text.index("14. RETURN DECISIONS"))
            self.assertIn("dead on arrival (DOA)", cur["acceptance_text"])
            self.assertIn("13A. AI GLASSES", pt.returns_html("optiwar.in"))
            com = pt.current("optiwar.com")
            self.assertEqual(com["returns"]["version"], "2026-09-15-com")
            self.assertNotIn("AI Glasses", com["returns"]["text"])

    def test_checkout_shows_doa_and_no_lens_deduction_for_ai_glasses(self):
        d = pt.disclosures_for([AIG], "optiwar.in")
        self.assertTrue(d["ai_glasses_doa_shown"])
        self.assertFalse(d["lens_deduction_shown"])
        d = pt.disclosures_for([AIG, FRAME], "optiwar.in")
        self.assertTrue(d["ai_glasses_doa_shown"] and d["lens_deduction_shown"])
        self.assertFalse(pt.disclosures_for([FRAME], "optiwar.in")["ai_glasses_doa_shown"])
        self.assertIn("ai_glasses_doa_shown", pt.DISCLOSURES)


class ReturnRequestTest(unittest.TestCase):
    def _form(self, reason):
        return {"reason": reason, "description": "They do not switch on at all after charging.",
                "declarations_version": rreq.DECLARATIONS_VERSION,
                "declarations": [str(i) for i in range(1, len(rreq.DECLARATIONS) + 1)]}

    def test_ai_glasses_orders_offer_doa_and_wrong_item_only(self):
        codes = [r["code"] for r in rreq.form_context(ai_glasses=True)["reasons"]]
        self.assertEqual(codes, ["DEAD_ON_ARRIVAL", "WRONG_ITEM"])
        self.assertNotIn("DEAD_ON_ARRIVAL", [r["code"] for r in rreq.form_context()["reasons"]])

    def test_the_server_refuses_any_other_reason(self):
        self.assertEqual(rreq._validate_form(self._form("DEAD_ON_ARRIVAL"), ai_glasses=True)[0], "DEAD_ON_ARRIVAL")
        with self.assertRaises(rp.ReversePickupError) as e:
            rreq._validate_form(self._form("OTHER"), ai_glasses=True)
        self.assertEqual(e.exception.code, "ai_glasses_doa_only")
        with self.assertRaises(rp.ReversePickupError):
            rreq._validate_form(self._form("DEAD_ON_ARRIVAL"))


class SoldCountTest(unittest.TestCase):
    def test_orders_refunded_in_full_are_not_sold(self):
        class Cur:
            def execute(self, sql, args=None):
                self.sql = sql

            def fetchall(self):
                return []
        cur = Cur()
        aig.sold_counts(cur)
        self.assertIn("NOT EXISTS (SELECT 1 FROM order_refunds", cur.sql)
        self.assertIn("refund_type = 'FULL'", cur.sql)
        self.assertIn("status IN ('PENDING', 'PROCESSED')", cur.sql)


if __name__ == "__main__":
    unittest.main()
