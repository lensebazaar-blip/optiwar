"""The automatic ₹250 reverse-pickup fee refund (phase 3c): a confirmed
manufacturing defect on a PAID fee refunds exactly that payment, once, through
one idempotency key; a failure stays REFUND PENDING and is retried."""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

import test_return_fee as trf  # noqa: E402

base, rp, rr, reship, reship_api = trf.base, trf.rp, trf.rr, trf.reship, trf.reship_api
import importlib  # noqa: E402
rrf = importlib.import_module(base.PKG + ".return_refund")
ops_refunds = importlib.import_module(base.PKG + ".ops_refunds")
RFT = trf.ReturnFeeTest

SUBJECT = "Optiwar Reverse-Pickup Fee Refunded"


class FakeRazorpay:
    """Razorpay's payment and refund records, as ops_refunds.RazorpayProvider
    reads and writes them."""

    def __init__(self):
        self.payments, self.refunds = {}, []
        self.posts = 0
        self.fail = 0
        self.timeout_after_create = 0

    def payment(self, pid):
        return dict(self.payments[pid])

    def existing_refund(self, pid, key):
        for r in self.refunds:
            if r["payment_id"] == pid and r["notes"].get("idempotency_key") == key:
                return dict(r)
        return None

    def refund(self, pid, amount, key, notes=None):
        found = self.existing_refund(pid, key)
        if found:
            return found
        self.posts += 1
        if self.fail:
            self.fail -= 1
            raise ops_refunds.ProviderError("provider HTTP 502")
        p = self.payments[pid]
        if p.get("amount_refunded", 0) + amount > p["amount"]:
            raise ops_refunds.ProviderError("provider HTTP 400: refund exceeds payment")
        ent = {"id": "rfnd_%d" % (len(self.refunds) + 1), "payment_id": pid, "amount": amount,
               "notes": dict(notes or {}), "status": "processed"}
        self.refunds.append(ent)
        p["amount_refunded"] = p.get("amount_refunded", 0) + amount
        p["status"] = "refunded"
        if self.timeout_after_create:
            self.timeout_after_create -= 1
            raise ops_refunds.ProviderError("provider unreachable: read timed out")
        return dict(ent)


class ReturnRefundTest(unittest.TestCase):

    setUpClass = classmethod(RFT.setUpClass.__func__)
    tearDownClass = classmethod(RFT.tearDownClass.__func__)
    def tearDown(self):
        trf.ReturnFeeTest.tearDown(self)
    _order, _client, _form, _submit = RFT._order, RFT._client, RFT._form, RFT._submit
    _ops, _decide, _case = RFT._ops, RFT._decide, RFT._case
    _approved, _post, _captured, _verify, _outbox = (RFT._approved, RFT._post, RFT._captured,
                                                     RFT._verify, RFT._outbox)

    _seq = [0]

    def setUp(self):
        trf.ReturnFeeTest.setUp(self)
        self.rzp = FakeRazorpay()
        self._provider = reship_api.refund_provider
        reship_api.refund_provider = lambda: self.rzp
        os.environ[rrf.ENABLED_ENV] = "1"

    def _cleanup(self):
        reship_api.refund_provider = self._provider
        os.environ.pop(rrf.ENABLED_ENV, None)

    def run(self, result=None):
        try:
            return super().run(result)
        finally:
            if hasattr(self, "_provider"):
                self._cleanup()

    # ------------------------------------------------------------ helpers

    def _received(self, fee="PAID"):
        """A return booked, picked up and physically received by Ops, with
        its fee PAID through the real payment path (or waived)."""
        cid, oid = self._approved()
        self._seq[0] += 1
        pid = "pay_RF%06d" % self._seq[0]
        if fee == "PAID":
            o = self._post(cid, oid, "create").get_json()
            self._captured(o["razorpay_order_id"], pid=pid)
            self.assertEqual(self._verify(cid, oid, o["razorpay_order_id"], pid=pid).status_code, 200)
            self.rzp.payments[pid] = dict(base.Stubs.provider_payments[pid], amount_refunded=0)
        else:
            r = self._ops(oid, "/fee/waive", {"operator": "ops", "reason_code": "GOODWILL"})
            self.assertEqual(r.status_code, 200, r.get_json())
        awb = "3612099%07d" % self._seq[0]
        r = self._ops(oid, "", dict(trf.trr.base_body(), awb=awb))
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self._ops(oid, "/received", {"operator": "ops", "awb": awb, "condition": "Intact"})
        self.assertEqual(r.status_code, 200, r.get_json())
        del self.mails[:]
        return cid, oid, pid

    def _inspect(self, oid, defect=True):
        return self._ops(oid, "/inspection", {"manufacturing_defect": defect, "operator": "ops"})

    def _refund_mails(self):
        return [m for m in self.mails if m[1] == SUBJECT]

    def _retry(self):
        return rrf.retry_pending(self.db, lambda: self.rzp, environ={rrf.ENABLED_ENV: "1",
                                                                    rrf.RETRY_MINUTES_ENV: "0"})

    def _age_attempt(self, oid, minutes=60):
        self.cur.execute("UPDATE reverse_pickup_cases SET fee_refund_last_attempt_at="
                         "NOW() - INTERVAL %s MINUTE WHERE order_id=%s", (minutes, oid))
        self.db.commit()

    # ------------------------------------------------------------ tests

    def test_a_confirmed_defect_on_a_paid_fee_refunds_exactly_that_payment_once(self):
        cid, oid, pid = self._received()
        r = self._inspect(oid)
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertEqual((j["fee_refund"], j["queue_state"]), (rrf.REFUNDED, "FEE_REFUNDED"))
        self.assertEqual(len(self.rzp.refunds), 1)
        ent = self.rzp.refunds[0]
        case = self._case(oid)
        self.assertEqual((ent["payment_id"], ent["amount"], ent["notes"]["idempotency_key"],
                          ent["notes"]["purpose"]),
                         (pid, 25000, "rpfee-refund:" + case["case_uuid"], rrf.PURPOSE))
        self.assertEqual((case["fee_state"], case["fee_refund_state"], case["fee_refund_id"],
                          int(case["fee_refunded_minor"]), int(case["fee_refund_attempts"])),
                         (rp.FEE_REFUNDED, rp.REFUND_DONE, "rfnd_1", 25000, 1))
        mails = self._refund_mails()
        self.assertEqual(len(mails), 1)
        self.assertIn("your ₹250 reverse-pickup fee has been refunded to your original payment "
                      "method (refund rfnd_1)", mails[0][2])
        self.assertIn("5–7 working days", mails[0][2])
        self.assertEqual(self._outbox(oid).count(rp.EV_FEE_REFUNDED), 1)
        self.assertEqual(self._outbox(oid).count(rp.EV_FEE_REFUND_FAILED), 0)

        state = self._ops(oid, method="get").get_json()
        self.assertEqual(state["fee"]["state"], rp.FEE_REFUNDED)
        self.assertEqual((state["fee"]["refunds"][0]["refund_id"],
                          state["fee"]["refunds"][0]["amount_minor"]), ("rfnd_1", 25000))
        card = rr.customer_cards(self.db, cid, [oid])[oid]
        self.assertEqual((card["state"], card["refund_id"], card["refunded"]),
                         (rr.CARD_FEE_REFUNDED, "rfnd_1", 250))

        # The same inspection again, a retry run and a direct call: nothing more.
        r = self._inspect(oid)
        self.assertEqual((r.status_code, r.get_json()["changed"]), (200, False))
        self.assertEqual(self._retry()["due"], 0)
        res = rrf.refund(self.db, case["case_uuid"], self.rzp, "test",
                         environ={rrf.ENABLED_ENV: "1"})
        self.assertEqual(res["outcome"], rrf.DUPLICATE)
        self.assertEqual((len(self.rzp.refunds), self.rzp.posts, len(self._refund_mails())), (1, 1, 1))
        self.assertEqual(self._outbox(oid).count(rp.EV_FEE_REFUNDED), 1)

    def test_an_unsent_defect_confirmed_notice_is_not_retried_after_the_refund(self):
        _c, oid, _p = self._received()
        real = rp.notify_case
        rp.notify_case = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("worker died"))
        try:
            j = self._inspect(oid).get_json()
        finally:
            rp.notify_case = real
        self.assertEqual((j["customer_notice"]["result"], j["fee_refund"]), ("failed", rrf.REFUNDED))
        self.assertEqual(self.mails, [])
        res = rp.retry_notices(self.db, environ=dict(os.environ, **{rp.NOTICE_RETRY_MINUTES_ENV: "0"}))
        self.assertEqual((res["stale"], res["sent"]), (1, 1))
        self.assertEqual([m[1] for m in self.mails], [SUBJECT])

    def test_no_refund_for_a_waived_fee_or_without_a_defect(self):
        _c, waived, _p = self._received(fee="WAIVED")
        j = self._inspect(waived).get_json()
        self.assertEqual((j["fee_refund"], j["queue_state"]), (None, "DEFECT_CONFIRMED"))
        self.assertIsNone(self._case(waived)["fee_refund_state"])

        _c, paid, _p = self._received()
        j = self._inspect(paid, defect=False).get_json()
        self.assertEqual((j["fee_refund"], j["queue_state"]), (None, "AWAITING_CUSTOMER_CONSENT"))
        self.assertEqual(self._case(paid)["fee_state"], rp.FEE_PAID)
        self.assertEqual(self._retry()["due"], 0)
        self.assertEqual((self.rzp.refunds, self._refund_mails()), ([], []))

    def test_an_unpaid_fee_is_never_refunded(self):
        _c, oid, _p = self._received()
        case = self._case(oid)
        self.cur.execute("UPDATE reverse_pickup_cases SET fee_state='DUE', razorpay_payment_id=NULL "
                         "WHERE id=%s", (case["id"],))
        self.db.commit()
        self._inspect(oid)
        self.assertIsNone(self._case(oid)["fee_refund_state"])
        # A refund marked owed on a fee that is not PAID is a person's exception.
        self.cur.execute("UPDATE reverse_pickup_cases SET fee_refund_state='PENDING' WHERE id=%s",
                         (case["id"],))
        self.db.commit()
        self.assertEqual(self._retry()[rrf.EXCEPTION], 1)
        self.assertEqual(self._case(oid)["fee_refund_state"], rp.REFUND_EXCEPTION)
        self.assertEqual(self.rzp.refunds, [])

    def test_a_provider_failure_stays_refund_pending_and_the_retry_refunds_once(self):
        _c, oid, pid = self._received()
        self.rzp.fail = 1
        j = self._inspect(oid).get_json()
        self.assertEqual((j["fee_refund"], j["queue_state"]), (rrf.FAILED, "REFUND_PENDING"))
        self.assertEqual(rp.QUEUE_LABELS["REFUND_PENDING"], "REFUND PENDING")
        case = self._case(oid)
        self.assertEqual((case["fee_state"], case["fee_refund_state"], case["fee_refund_error"]),
                         (rp.FEE_PAID, rp.REFUND_FAILED, "provider HTTP 502"))
        self.assertEqual(self._outbox(oid).count(rp.EV_FEE_REFUND_FAILED), 1)
        self.assertEqual(self._refund_mails(), [])

        # Not due before the retry interval.
        out = rrf.retry_pending(self.db, lambda: self.rzp, environ={rrf.ENABLED_ENV: "1"})
        self.assertEqual(out["due"], 0)
        self.rzp.fail = 1
        self._age_attempt(oid)
        self.assertEqual(self._retry()[rrf.FAILED], 1)
        self.assertEqual(self._outbox(oid).count(rp.EV_FEE_REFUND_FAILED), 1)
        self._age_attempt(oid)
        self.assertEqual(self._retry()[rrf.REFUNDED], 1)
        case = self._case(oid)
        self.assertEqual((case["fee_state"], int(case["fee_refund_attempts"])), (rp.FEE_REFUNDED, 3))
        self.assertEqual(len(self.rzp.refunds), 1)
        self.assertEqual(self._outbox(oid).count(rp.EV_FEE_REFUNDED), 1)
        # The notice is owed once and sent by the notice retry, not repeated.
        rp.retry_notices(self.db, environ={rp.CUSTOMER_ENV: "true",
                                           rp.NOTICE_RETRY_MINUTES_ENV: "0"})
        rp.retry_notices(self.db, environ={rp.CUSTOMER_ENV: "true",
                                           rp.NOTICE_RETRY_MINUTES_ENV: "0"})
        self.assertEqual(len(self._refund_mails()), 1)

    def test_a_timeout_after_razorpay_created_the_refund_does_not_refund_twice(self):
        _c, oid, _pid = self._received()
        self.rzp.timeout_after_create = 1
        self.assertEqual(self._inspect(oid).get_json()["fee_refund"], rrf.FAILED)
        self.assertEqual(len(self.rzp.refunds), 1)
        self._age_attempt(oid)
        self.assertEqual(self._retry()[rrf.REFUNDED], 1)
        self.assertEqual((len(self.rzp.refunds), self.rzp.posts), (1, 1))
        self.assertEqual(self._case(oid)["fee_refund_id"], "rfnd_1")

    def test_an_interrupted_request_is_retried_only_once_stale(self):
        _c, oid, _pid = self._received()
        os.environ.pop(rrf.ENABLED_ENV)
        self.assertEqual(self._inspect(oid).get_json()["fee_refund"], rrf.DISABLED)
        self.cur.execute("UPDATE reverse_pickup_cases SET fee_refund_state='REQUESTING', "
                         "fee_refund_last_attempt_at=NOW() WHERE order_id=%s", (oid,))
        self.db.commit()
        self.assertEqual(self._retry()["due"], 0)
        self._age_attempt(oid, minutes=rrf.STALE_MINUTES + 1)
        self.assertEqual(self._retry()[rrf.REFUNDED], 1)
        self.assertEqual(len(self.rzp.refunds), 1)

    def test_switched_off_nothing_calls_razorpay_and_the_case_waits(self):
        _c, oid, _pid = self._received()
        os.environ.pop(rrf.ENABLED_ENV)
        j = self._inspect(oid).get_json()
        self.assertEqual((j["fee_refund"], j["queue_state"]), (rrf.DISABLED, "REFUND_PENDING"))
        out = rrf.retry_pending(self.db, lambda: self.rzp, environ={})
        self.assertEqual((out["off"], out["open"] >= 1), (True, True))
        self.assertEqual((self.rzp.refunds, self.rzp.posts), ([], 0))
        self.assertEqual(self._retry()[rrf.REFUNDED], 1)

    def test_a_payment_that_is_not_this_fee_is_an_exception_not_a_refund(self):
        cases = []
        for change in ({"amount": 24900}, {"currency": "USD"}, {"order_id": "order_OTHER"},
                       {"status": "authorized"}, {"amount_refunded": 25000}):
            _c, oid, pid = self._received()
            self.rzp.payments[pid].update(change)
            j = self._inspect(oid).get_json()
            self.assertEqual((j["fee_refund"], j["queue_state"]), (rrf.EXCEPTION, "REFUND_EXCEPTION"),
                             change)
            case = self._case(oid)
            self.assertEqual((case["fee_state"], case["fee_refund_state"]),
                             (rp.FEE_PAID, rp.REFUND_EXCEPTION))
            self.assertEqual(self._outbox(oid).count(rp.EV_FEE_REFUND_FAILED), 1)
            cases.append(oid)
        self.assertEqual((self.rzp.refunds, self.rzp.posts, self._refund_mails()), ([], 0, []))
        self.assertEqual(self._retry()["due"], 0)

    def test_a_refund_entity_for_another_amount_is_refused(self):
        _c, oid, _pid = self._received()
        real = self.rzp.refund
        self.rzp.refund = lambda pid, amount, key, notes=None: dict(real(pid, amount, key, notes),
                                                                    amount=100)
        self.assertEqual(self._inspect(oid).get_json()["fee_refund"], rrf.EXCEPTION)
        self.assertEqual(self._case(oid)["fee_state"], rp.FEE_PAID)

    def test_an_inspection_recorded_before_this_code_is_refunded_by_the_retry(self):
        _c, oid, _pid = self._received()
        os.environ.pop(rrf.ENABLED_ENV)
        self._inspect(oid)
        self.cur.execute("UPDATE reverse_pickup_cases SET fee_refund_state=NULL, fee_refund_key=NULL "
                         "WHERE order_id=%s", (oid,))
        self.db.commit()
        self.assertEqual(self._retry()[rrf.REFUNDED], 1)
        self.assertEqual(self.rzp.refunds[0]["notes"]["idempotency_key"],
                         "rpfee-refund:" + self._case(oid)["case_uuid"])


del RFT

if __name__ == "__main__":
    unittest.main()
