"""A return's refund is calculated from the accepted policy, never typed in.

Pure tests need no database. The ledger tests reuse test_refunds' fixtures and
skip when no test database is reachable.

    python3 -m unittest tests.test_return_assessment
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.test_refunds import (  # noqa: E402
    FakeProvider, RefundTest, refunds, _load)

ra = _load("return_assessment")

INSPECTION = dict(
    inspection_reason="Lenses edged to this frame; frame scratched on left temple.",
    inspection_evidence="photos/RET-1/{front,left,rx}.jpg; inspection sheet #77",
    inspection_findings={'is_supplied_item': True, 'used': True,
                         'non_recoverable_customization': True},
)


def facts(paid=100000, refundable=None, storefront='in', currency='INR'):
    return {'order_id': 'X-1', 'storefront': storefront, 'payment_status': 'PAID',
            'currency': currency, 'captured_minor': paid,
            'max_refundable_minor': paid if refundable is None else refundable}


IN_ACC = {'site': 'in', 'returns_policy_version': '2026-09-15-in',
          'returns_sha256': 'a' * 64}
COM_ACC = {'site': 'com', 'returns_policy_version': '2026-09-15-com',
           'returns_sha256': 'b' * 64}


class ProposeTest(unittest.TestCase):

    def rejects(self, code, *a, **kw):
        with self.assertRaises(ra.AssessmentRejected) as ctx:
            ra.propose(*a, **kw)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_formula_paid_minus_lens_minus_logistics(self):
        p = ra.propose(facts(100000), IN_ACC, ra.DISCRETIONARY,
                       spectacle_value_minor=60000, lens_deduction_minor=25000,
                       reverse_logistics_minor=15000, **INSPECTION)
        self.assertEqual(p['proposed_refund_minor'], 100000 - 25000 - 15000)
        self.assertEqual(p['lens_deduction_cap_minor'], 30000)
        self.assertEqual(p['lens_deduction_cap_pct'], 50)
        self.assertEqual(p['returns_policy_version'], '2026-09-15-in')
        self.assertEqual(p['returns_sha256'], 'a' * 64)
        self.assertTrue(p['inspection_findings']['used'])
        self.assertFalse(p['inspection_findings']['defective'])

    def test_lens_deduction_above_half_the_spectacle_value_is_refused(self):
        self.rejects('lens_deduction_exceeds_cap', facts(), IN_ACC, ra.DISCRETIONARY,
                     spectacle_value_minor=60000, lens_deduction_minor=30001,
                     **INSPECTION)
        p = ra.propose(facts(), IN_ACC, ra.DISCRETIONARY,
                       spectacle_value_minor=60000, lens_deduction_minor=30000,
                       **INSPECTION)
        self.assertEqual(p['proposed_refund_minor'], 70000)

    def test_cap_rounds_in_the_customers_favour(self):
        self.assertEqual(ra.cap_minor(33333, 50), 16666)

    def test_lens_deduction_needs_the_spectacle_value_and_it_cannot_exceed_paid(self):
        self.rejects('spectacle_value_required', facts(), IN_ACC, ra.DISCRETIONARY,
                     lens_deduction_minor=1, **INSPECTION)
        self.rejects('spectacle_value_exceeds_paid', facts(100000), IN_ACC,
                     ra.DISCRETIONARY, spectacle_value_minor=100001,
                     lens_deduction_minor=1, **INSPECTION)

    def test_a_deduction_without_inspection_is_refused(self):
        self.rejects('deduction_requires_inspection', facts(), IN_ACC, ra.DISCRETIONARY,
                     spectacle_value_minor=60000, lens_deduction_minor=100)
        self.rejects('deduction_requires_inspection', facts(), IN_ACC, ra.DISCRETIONARY,
                     reverse_logistics_minor=100, inspection_reason='short',
                     inspection_evidence='x')
        self.rejects('deduction_requires_findings', facts(), IN_ACC, ra.DISCRETIONARY,
                     reverse_logistics_minor=100,
                     inspection_reason=INSPECTION['inspection_reason'],
                     inspection_evidence='sheet #1')

    def test_no_deduction_needs_no_inspection(self):
        p = ra.propose(facts(), IN_ACC, ra.DISCRETIONARY)
        self.assertEqual(p['proposed_refund_minor'], 100000)
        self.assertIsNone(p['inspection_findings'])

    def test_defect_and_incorrect_supply_carry_no_deduction(self):
        for claim in (ra.INCORRECT_SUPPLY, ra.MANUFACTURING_DEFECT):
            self.rejects('deduction_not_for_%s' % claim.lower(), facts(), IN_ACC,
                         claim, spectacle_value_minor=60000,
                         lens_deduction_minor=1, **INSPECTION)
            self.rejects('deduction_not_for_%s' % claim.lower(), facts(), IN_ACC,
                         claim, reverse_logistics_minor=1, **INSPECTION)
            self.assertEqual(ra.propose(facts(), IN_ACC, claim)['proposed_refund_minor'],
                             100000)

    def test_com_discretionary_return_is_non_refundable_without_written_override(self):
        self.rejects('com_non_refundable', facts(storefront='com'), COM_ACC,
                     ra.DISCRETIONARY)
        self.rejects('com_non_refundable', facts(storefront='com'), COM_ACC,
                     ra.DISCRETIONARY, policy_override_reason='ok')
        p = ra.propose(facts(storefront='com', currency='EUR'), COM_ACC, ra.DISCRETIONARY,
                       policy_override_reason='Director-approved goodwill, ticket KET-4410')
        self.assertEqual(p['proposed_refund_minor'], 100000)
        self.assertEqual(p['site'], 'com')

    def test_com_defect_and_incorrect_supply_stay_refundable(self):
        for claim in (ra.INCORRECT_SUPPLY, ra.MANUFACTURING_DEFECT):
            p = ra.propose(facts(storefront='com'), COM_ACC, claim)
            self.assertEqual(p['proposed_refund_minor'], 100000)

    def test_override_is_refused_where_it_does_not_apply(self):
        self.rejects('override_not_applicable', facts(), IN_ACC, ra.DISCRETIONARY,
                     policy_override_reason='x' * 30)
        self.rejects('override_not_applicable', facts(storefront='com'), COM_ACC,
                     ra.MANUFACTURING_DEFECT, policy_override_reason='x' * 30)

    def test_site_comes_from_the_accepted_snapshot_then_the_storefront(self):
        self.assertEqual(ra.propose(facts(storefront='www.optiwar.in'), None,
                                    ra.DISCRETIONARY)['site'], 'in')
        self.rejects('com_non_refundable', facts(storefront='www.optiwar.com'), None,
                     ra.DISCRETIONARY)
        self.rejects('com_non_refundable', facts(storefront='in'), COM_ACC,
                     ra.DISCRETIONARY)

    def test_unpaid_bad_claim_negative_and_over_refundable_are_refused(self):
        self.rejects('order_not_paid', dict(facts(), payment_status='UNPAID'),
                     IN_ACC, ra.DISCRETIONARY)
        self.rejects('bad_claim_type', facts(), IN_ACC, 'GOODWILL')
        self.rejects('reverse_logistics_minor_negative', facts(), IN_ACC,
                     ra.DISCRETIONARY, reverse_logistics_minor=-1, **INSPECTION)
        self.rejects('lens_deduction_minor_not_integer', facts(), IN_ACC,
                     ra.DISCRETIONARY, spectacle_value_minor=60000,
                     lens_deduction_minor=10.5, **INSPECTION)
        self.rejects('proposed_exceeds_refundable', facts(100000, refundable=40000),
                     IN_ACC, ra.DISCRETIONARY)
        self.rejects('deductions_exceed_paid', facts(1000), IN_ACC, ra.DISCRETIONARY,
                     reverse_logistics_minor=2000, **INSPECTION)

    def test_refund_must_be_exactly_the_assessed_amount_once(self):
        a = {'order_id': 'O1', 'proposed_refund_minor': 70000, 'currency': 'INR',
             'refund_id': None}
        ra.check_refund_matches(a, 'O1', 70000, 'INR')
        for args, code in (((a, 'O1', 69999, 'INR'), 'amount_not_the_assessment'),
                           ((a, 'O1', 70000, 'EUR'), 'amount_not_the_assessment'),
                           ((a, 'O2', 70000, 'INR'), 'assessment_order_mismatch'),
                           ((None, 'O1', 70000, 'INR'), 'assessment_not_found'),
                           ((dict(a, refund_id=5), 'O1', 70000, 'INR'),
                            'assessment_already_executed')):
            with self.assertRaises(ra.AssessmentRejected) as ctx:
                ra.check_refund_matches(*args)
            self.assertEqual(ctx.exception.code, code)


class LedgerTest(RefundTest):
    """Against the test database: an assessment pays out once, at its amount."""

    def tearDown(self):
        for order_id in self._orders:
            self.cur.execute("DELETE FROM return_assessments WHERE order_id=%s",
                             (order_id,))
        super().tearDown()

    def _assess(self, order_id, provider, **kw):
        f = refunds.preview(self.cur, order_id, provider)
        p = ra.propose(f, None, kw.pop('claim_type', ra.DISCRETIONARY), **kw)
        aid = ra.record(self.cur, p, 'ops@lensbazaar', 'eu-ops')
        self.db.commit()
        return aid, p

    def test_return_received_refund_requires_an_assessment(self):
        refunds._SCHEMA_READY = False
        refunds.ensure_schema(self.cur)
        order_id = self._order()
        with self.assertRaises(refunds.RefundRejected) as ctx:
            self._refund(order_id, FakeProvider(), reason='RETURN_RECEIVED')
        self.assertEqual(ctx.exception.code, 'assessment_required')

    def test_the_refund_is_the_assessed_amount_and_the_assessment_is_sealed(self):
        refunds._SCHEMA_READY = False
        refunds.ensure_schema(self.cur)
        order_id = self._order()
        provider = FakeProvider(amount=99900)
        aid, p = self._assess(order_id, provider, spectacle_value_minor=60000,
                              lens_deduction_minor=20000, **INSPECTION)
        self.assertEqual(p['proposed_refund_minor'], 79900)

        with self.assertRaises(refunds.RefundRejected) as ctx:
            refunds.execute(self.db, order_id, amount_minor=99900, currency='INR',
                            reason_code='RETURN_RECEIVED', comment=None,
                            idempotency_key=self._key(order_id), requested_by='ops',
                            service_identity='eu-ops', approved_message=None,
                            provider=provider, assessment_id=aid)
        self.assertEqual(ctx.exception.code, 'amount_not_the_assessment')
        self.assertEqual(provider.refund_calls, [])

        row = refunds.execute(self.db, order_id, amount_minor=79900, currency='INR',
                              reason_code='RETURN_RECEIVED', comment=None,
                              idempotency_key=self._key(order_id), requested_by='ops',
                              service_identity='eu-ops', approved_message=None,
                              provider=provider, assessment_id=aid)
        self.assertEqual(row['status'], refunds.PROCESSED)
        stored = ra.get(self.cur, aid)
        self.assertEqual(stored['refund_id'], row['refund_id'])
        self.assertIsNotNone(stored['executed_at'])
        self.assertEqual(stored['lens_deduction_cap_minor'], 30000)
        self.assertEqual(len(ra.for_order(self.cur, order_id)), 1)

        with self.assertRaises(refunds.RefundRejected) as ctx:
            refunds.execute(self.db, order_id, amount_minor=79900, currency='INR',
                            reason_code='RETURN_RECEIVED', comment=None,
                            idempotency_key=self._key(order_id), requested_by='ops',
                            service_identity='eu-ops', approved_message=None,
                            provider=provider, assessment_id=aid)
        self.assertIn(ctx.exception.code,
                      ('assessment_already_executed', 'amount_exceeds_refundable'))
        self.assertEqual(len(provider.refund_calls), 1)

    def test_a_com_order_without_acceptance_row_is_non_refundable_by_storefront(self):
        refunds._SCHEMA_READY = False
        refunds.ensure_schema(self.cur)
        order_id = self._order(site='www.optiwar.com')
        f = refunds.preview(self.cur, order_id, FakeProvider(currency='EUR'))
        with self.assertRaises(ra.AssessmentRejected) as ctx:
            ra.propose(f, None, ra.DISCRETIONARY)
        self.assertEqual(ctx.exception.code, 'com_non_refundable')


del RefundTest  # inherited fixtures only; test_refunds runs its own cases


if __name__ == '__main__':
    unittest.main()
