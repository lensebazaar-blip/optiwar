"""A customer accepts a specific, versioned policy, and the order keeps it.

optiwar.com orders are absolutely non-returnable; optiwar.in orders have the
discretionary-return / up-to-50% customized-lens text. The box is never
pre-checked, the server refuses a checkout without it or with a stale version,
and the versions accepted are sealed against the order so a later policy
change cannot rewrite what an old order was placed under.
"""
import datetime
import importlib.util
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name + "_under_test", os.path.join(REPO, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pt = _load("policy_terms")


class FakeCursor:
    """Enough of a DictCursor for policy_versions / order_terms_acceptance."""

    def __init__(self):
        self.versions = {}      # (kind, site, sha) -> body
        self.acceptance = {}    # order_id -> row
        self._rows = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("CREATE TABLE"):
            return
        if s.startswith("INSERT IGNORE INTO policy_versions"):
            kind, site, version, sha, body = params
            self.versions.setdefault((kind, site, sha), (version, body))
            return
        if s.startswith("INSERT IGNORE INTO order_terms_acceptance"):
            keys = ("order_id", "checkout_token", "site", "terms_version",
                    "terms_sha256", "returns_policy_version", "returns_sha256",
                    "acceptance_text_sha256", "accepted_at", "customer_id",
                    "ip_address", "disclosures")
            row = dict(zip(keys, params))
            self.acceptance.setdefault(row["order_id"], row)
            return
        if s.startswith("SELECT order_id, site"):
            row = self.acceptance.get(params[0])
            self._rows = [dict(row)] if row else []
            return
        if s.startswith("SELECT body FROM policy_versions"):
            kind, sha = params
            hit = [b for (k, _s, h), (_v, b) in self.versions.items()
                   if k == kind and h == sha]
            self._rows = [{"body": hit[0]}] if hit else []
            return
        raise AssertionError("unexpected SQL: " + s)

    def fetchone(self):
        return self._rows[0] if self._rows else None


FRAME = {"product_name": "Aviator", "product_category": "Spectacles Frame",
         "ATC_total": 2000, "server_total_price": 1500, "order_quantity": 1,
         "recommendations": "Blue-cut 1.56", "addon_1_name": "Case", "addon_1_price": 200}
PLAIN = {"product_name": "Sun", "product_category": "Sunglasses",
         "ATC_total": 900, "server_total_price": 0}
LENS = {"product_name": "Precision 1", "vertical": "CONTACT_LENS",
        "product_category": "Contact Lenses", "ATC_total": 26.95}


def form(checked=True, version=None, site="optiwar.com"):
    f = {}
    if checked:
        f[pt.FORM_FIELD] = "1"
    f[pt.FORM_VERSION_FIELD] = version if version is not None \
        else pt.current(site)["returns"]["version"]
    return f


class SiteAwareText(unittest.TestCase):
    def test_site_key(self):
        for host in ("in.optiwar.com", "optiwar.in", "www.optiwar.in", "in", "india"):
            self.assertEqual(pt.site_key(host), pt.SITE_IN, host)
        for host in ("optiwar.com", "www.optiwar.com", "com", "", None):
            self.assertEqual(pt.site_key(host), pt.SITE_COM, host)

    def test_com_is_absolutely_non_returnable(self):
        com = pt.current("optiwar.com")
        self.assertIn("absolutely non-returnable and non-refundable", com["returns"]["text"])
        self.assertIn("non-returnable and non-refundable", com["acceptance_text"])
        self.assertNotIn("50%", com["acceptance_text"])

    def test_in_has_discretionary_return_and_cap(self):
        ind = pt.current("optiwar.in")
        self.assertIn("50%", ind["returns"]["text"])
        self.assertIn("up to 50%", ind["acceptance_text"])
        self.assertNotIn("absolutely non-returnable", ind["returns"]["text"])
        self.assertIn("reverse", ind["returns"]["text"].lower())

    def test_versions_differ_per_site_and_hash_the_text(self):
        com, ind = pt.current("com"), pt.current("in")
        self.assertNotEqual(com["returns"]["sha256"], ind["returns"]["sha256"])
        self.assertNotEqual(com["returns"]["version"], ind["returns"]["version"])
        self.assertEqual(com["returns"]["sha256"], pt.sha(com["returns"]["text"]))
        self.assertEqual(com["terms"]["sha256"], pt.sha(pt.terms_text()))

    def test_terms_text_is_the_deployed_template(self):
        self.assertIn("<h2", pt.terms_text())

    def test_render_html_escapes_and_anchors(self):
        html = pt.returns_html("in")
        self.assertIn('id="returns-policy"', html)
        self.assertNotIn("<script", html)
        self.assertIn("<h3>", html)


class ReturnedParcelClause(unittest.TestCase):
    def test_in_policy_is_a_new_version_with_the_holding_clause(self):
        ind = pt.current("in")
        self.assertEqual(ind["returns"]["version"], "2026-09-27-in")
        text = pt.RETURNS_TEXT["in"]
        self.assertIn("FAILED DELIVERY, RETURNED PACKAGES AND UNCLAIMED PARCELS", text)
        self.assertIn("sixty (60) days", text)
        self.assertIn("physically confirmed receipt", text)
        self.assertIn("Rs 250", text)
        self.assertIn("treated as abandoned", text)
        # the clause is India-only; .com stays on its sealed version
        self.assertNotIn("UNCLAIMED PARCELS", pt.RETURNS_TEXT["com"])
        self.assertEqual(pt.current("com")["returns"]["version"], "2026-09-15-com")


class Disclosures(unittest.TestCase):
    def test_in_customized_spectacles(self):
        d = pt.disclosures_for([FRAME], "optiwar.in")
        self.assertEqual(d, {"lens_deduction_shown": True, "reverse_charge_shown": True,
                             "contact_lens_hygiene_shown": False,
                             "international_non_returnable_shown": False,
                             "returned_parcel_holding_shown": True})

    def test_in_plain_frame_has_no_lens_deduction(self):
        d = pt.disclosures_for([PLAIN], "optiwar.in")
        self.assertFalse(d["lens_deduction_shown"])
        self.assertTrue(d["reverse_charge_shown"])

    def test_com_never_shows_the_in_deduction(self):
        d = pt.disclosures_for([FRAME, LENS], "optiwar.com")
        self.assertEqual(d, {"lens_deduction_shown": False, "reverse_charge_shown": False,
                             "contact_lens_hygiene_shown": True,
                             "international_non_returnable_shown": True,
                             "returned_parcel_holding_shown": False})

    def test_contact_lens_hygiene_on_in_too(self):
        self.assertTrue(pt.disclosures_for([LENS], "in")["contact_lens_hygiene_shown"])

    def test_spectacle_summary(self):
        (s,) = pt.spectacle_summary([FRAME, LENS])
        self.assertEqual((s["frame"], s["lens"], s["total"]), (2000.0, 1500.0, 3500.0))
        self.assertTrue(s["customized"])
        self.assertEqual(s["others"], [("Case", 200.0)])
        (p,) = pt.spectacle_summary([PLAIN])
        self.assertFalse(p["customized"])

    def test_checkout_context_carries_field_names_and_cap(self):
        ctx = pt.checkout_context([FRAME], "optiwar.in")
        self.assertEqual(ctx["lens_deduction_cap_percent"], 50)
        self.assertEqual(ctx["terms_field"], pt.FORM_FIELD)
        self.assertFalse(ctx["policy_is_international"])
        self.assertTrue(pt.checkout_context([], "optiwar.com")["policy_is_international"])


class Acceptance(unittest.TestCase):
    def test_unchecked_is_refused(self):
        self.assertFalse(pt.accepted(form(checked=False), "optiwar.com"))
        with self.assertRaises(pt.NotAccepted):
            pt.require({}, "optiwar.com")

    def test_stale_or_missing_version_is_refused(self):
        self.assertFalse(pt.accepted(form(version="2025-01-01-com"), "optiwar.com"))
        self.assertFalse(pt.accepted(form(version=""), "optiwar.com"))
        # the other site's version is not this site's
        self.assertFalse(pt.accepted(form(site="optiwar.in"), "optiwar.com"))

    def test_checked_current_version_is_accepted_per_site(self):
        self.assertTrue(pt.accepted(form(site="optiwar.com"), "optiwar.com"))
        self.assertTrue(pt.accepted(form(site="optiwar.in"), "in.optiwar.com"))
        for v in ("on", "true", "yes"):
            f = form(site="optiwar.in")
            f[pt.FORM_FIELD] = v
            self.assertTrue(pt.accepted(f, "optiwar.in"), v)

    def test_checkbox_default_is_unchecked_in_template(self):
        with open(os.path.join(REPO, "templates", "checkout.html"), encoding="utf-8") as fh:
            html = fh.read()
        i = html.index('id="owTermsAccepted"')
        tag = html[i:html.index(">", i)]
        self.assertNotIn("checked", tag)
        self.assertIn('name="{{ terms_field }}"', tag)


class Snapshot(unittest.TestCase):
    def test_record_seals_versions_and_disclosures(self):
        cur = FakeCursor()
        when = datetime.datetime(2026, 9, 15, 7, 0)
        out = pt.record(cur, "ABCDEF-123456", "in.optiwar.com", [FRAME],
                        checkout_token="tok", customer_id=7, ip_address="1.2.3.4", now=when)
        row = cur.acceptance["ABCDEF-123456"]
        ind = pt.current("in")
        self.assertEqual(row["site"], "in")
        self.assertEqual(row["returns_sha256"], ind["returns"]["sha256"])
        self.assertEqual(row["terms_sha256"], ind["terms"]["sha256"])
        self.assertEqual(row["accepted_at"], when)
        self.assertEqual(row["checkout_token"], "tok")
        self.assertEqual(json.loads(row["disclosures"]),
                         {"lens_deduction_shown": "YES", "reverse_charge_shown": "YES",
                          "contact_lens_hygiene_shown": "NO",
                          "international_non_returnable_shown": "NO",
                          "returned_parcel_holding_shown": "YES"})
        self.assertEqual(out["returns_policy_version"], ind["returns"]["version"])
        # the full text is sealed so the accepted document can be reproduced
        self.assertEqual(pt.sealed_text(cur, "returns", ind["returns"]["sha256"]),
                         ind["returns"]["text"])
        self.assertEqual(pt.sealed_text(cur, "terms", ind["terms"]["sha256"]), pt.terms_text())

    def test_record_is_write_once(self):
        cur = FakeCursor()
        pt.record(cur, "X-1", "optiwar.com", [LENS], now=datetime.datetime(2026, 1, 1))
        pt.record(cur, "X-1", "optiwar.in", [FRAME], now=datetime.datetime(2026, 2, 2))
        self.assertEqual(cur.acceptance["X-1"]["site"], "com")

    def test_old_order_keeps_old_version_after_policy_change(self):
        cur = FakeCursor()
        pt.record(cur, "OLD-1", "optiwar.in", [FRAME])
        old = dict(cur.acceptance["OLD-1"])
        saved = pt.RETURNS_TEXT[pt.SITE_IN]
        try:
            pt.RETURNS_TEXT[pt.SITE_IN] = saved + "\n\n99. A NEW CLAUSE."
            pt.record(cur, "NEW-1", "optiwar.in", [FRAME])
        finally:
            pt.RETURNS_TEXT[pt.SITE_IN] = saved
        new = cur.acceptance["NEW-1"]
        self.assertNotEqual(old["returns_sha256"], new["returns_sha256"])
        self.assertEqual(pt.for_order(cur, "OLD-1")["returns_sha256"], old["returns_sha256"])
        self.assertIn("99. A NEW CLAUSE.", pt.sealed_text(cur, "returns", new["returns_sha256"]))
        self.assertNotIn("99. A NEW CLAUSE.", pt.sealed_text(cur, "returns", old["returns_sha256"]))

    def test_for_order_decodes_disclosures_and_is_none_for_pre_policy(self):
        cur = FakeCursor()
        self.assertIsNone(pt.for_order(cur, "NOPE"))
        pt.record(cur, "Y-1", "optiwar.com", [LENS])
        acc = pt.for_order(cur, "Y-1")
        self.assertEqual(acc["disclosures"]["international_non_returnable_shown"], "YES")


class ConfirmationEmail(unittest.TestCase):
    def test_line_and_links(self):
        line, terms, returns = pt.confirmation_line("optiwar.in", None)
        self.assertEqual(line, "Your order was placed subject to the Optiwar Terms & "
                               "Conditions and Returns, Replacements & Limited Warranty "
                               "Policy accepted at checkout.")
        self.assertEqual(terms, "https://optiwar.in/terms_and_conditions")
        self.assertTrue(returns.endswith("#returns-policy"))

    def test_line_names_versions_when_known(self):
        line, _, _ = pt.confirmation_line("optiwar.com", {
            "terms_version": "2026-09-15", "returns_policy_version": "2026-09-15-com"})
        self.assertIn("Terms 2026-09-15", line)
        self.assertIn("Returns policy 2026-09-15-com", line)


if __name__ == "__main__":
    unittest.main()
