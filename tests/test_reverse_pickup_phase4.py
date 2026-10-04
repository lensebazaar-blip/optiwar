"""Reverse pickup phase 4a: the product shipped back to the customer, then a
person completes the case with its outcome. Nothing is inferred from a
shipment, nothing is shown to the customer before Ops recorded it."""
import importlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tests import test_reverse_pickup as trp  # noqa: E402

PKG = trp.PKG
rp = trp.rp
customer_orders = trp.customer_orders
ra = importlib.import_module(PKG + ".return_assistant")
assess = importlib.import_module(PKG + ".return_assessment")
rreq = importlib.import_module(PKG + ".return_request")

DTDC_AWB = "7X119057819"


RPT = trp.ReversePickupTest


class Phase4Test(unittest.TestCase):
    """Borrows the reverse-pickup harness without re-running its tests."""

    @classmethod
    def setUpClass(cls):
        trp.ReversePickupTest.setUpClass.__func__(cls)
        cur = cls.db.cursor()
        cur.execute(assess.SCHEMA)
        cls.db.commit()

    tearDownClass = classmethod(RPT.tearDownClass.__func__)
    _awb_seq = RPT._awb_seq
    _order, _case, _post, _body, _history, _get, _outbox = (RPT._order, RPT._case, RPT._post, RPT._body,
                                                           RPT._history, RPT._get, RPT._outbox)
    _booked, _received, _events, _age_notices = RPT._booked, RPT._received, RPT._events, RPT._age_notices

    def setUp(self):
        trp.ReversePickupTest.setUp(self)

    def tearDown(self):
        for oid in self._orders:
            self.cur.execute("DELETE FROM return_assessments WHERE order_id=%s", (oid,))
        self.db.commit()
        trp.ReversePickupTest.tearDown(self)

    # ----------------------------------------------------------- helpers

    def _inspected(self, defect, fee="PAID"):
        oid, awb = self._booked(fee)
        self._received(oid, awb)
        r = self._post(oid, {"operator": "qc", "manufacturing_defect": defect}, path="/inspection")
        self.assertEqual(r.status_code, 200, r.get_json())
        del self.mails[:]
        return oid, awb

    def _ship(self, oid, **kw):
        body = {"operator": "dispatch", "shipment_type": "REPLACEMENT", "courier": "DTDC", "awb": DTDC_AWB}
        body.update(kw)
        return self._post(oid, body, path="/forward-shipped")

    def _complete(self, oid, outcome, **kw):
        body = {"operator": "ops-lead", "outcome": outcome}
        body.update(kw)
        return self._post(oid, body, path="/complete")

    def _cid(self, oid):
        self.cur.execute("SELECT customer_id FROM orders WHERE order_id=%s", (oid,))
        return self.cur.fetchone()["customer_id"]

    def _card(self, oid):
        cid = self._cid(oid)
        orders = [{"order_id": oid, "stage_label": "Delivered", "stage_tone": "shipped"}]
        customer_orders.attach_reverse_pickup(
            orders, rp.latest_for_customer(self.db, cid),
            request_cards=rreq.customer_cards(self.db, cid, [oid]))
        return orders[0]

    def _model(self, oid):
        return ra.read_model(self.db, self._cid(oid), "optiwar.in", environ=dict(os.environ))

    # ------------------------------------------------------------- tests

    def test_a_replacement_is_recorded_once_told_once_and_the_awb_is_checked(self):
        oid, pickup_awb = self._inspected(defect=True, fee="WAIVED")
        for body, code in (({"shipment_type": "GIFT"}, "invalid_shipment_type"),
                           ({"courier": ""}, "courier_required"),
                           ({"awb": ""}, "invalid_awb"),
                           ({"awb": "12"}, "invalid_awb"),
                           ({"courier": "Delhivery", "awb": "7X119057819"}, "invalid_awb"),
                           ({"shipment_type": "ORIGINAL_RETURNED"}, "shipment_type_mismatch"),
                           ({"awb": trp.FORWARD}, "awb_is_original_shipment"),
                           ({"courier": "Delhivery", "awb": pickup_awb}, "awb_is_reverse_pickup")):
            r = self._ship(oid, **body)
            self.assertEqual(r.get_json()["error"], code, body)
        self.assertEqual(self.mails, [])
        state = self._get("/ops/api/shipments/%s/reverse-pickup" % oid).get_json()
        self.assertIsNone(state["forward"])
        self.assertIsNone(state["forward_shipment"]["to_customer"])

        r = self._ship(oid, awb="7x119057819 ", shipped_at="2026-10-03T11:00:00+05:30")
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertTrue(j["changed"])
        self.assertEqual(j["queue_state"], "FORWARD_SHIPPED")
        self.assertEqual((j["forward"]["type"], j["forward"]["courier"], j["forward"]["awb"],
                          j["forward"]["shipped_by"]),
                         ("REPLACEMENT", "DTDC", DTDC_AWB, "ops-api-token:dispatch"))
        self.assertEqual(j["forward"]["track_url"], "https://www.dtdc.com/track")
        self.assertEqual(j["forward_shipment"]["original"]["awb"], trp.FORWARD)
        self.assertIsNone(j["completion"])
        self.assertEqual(j["customer_notice"], {"notice": "forward_shipped", "result": "sent"})
        self.assertEqual([m[1] for m in self.mails], ["Optiwar Return: Shipment on Its Way to You"])
        text = self.mails[0][2]
        self.assertIn("A replacement product for your order is on its way to you.\nCourier: DTDC\nAWB: %s\n"
                      "Track: https://www.dtdc.com/track\nThere is nothing to pay" % DTDC_AWB, text)
        self.assertNotIn("Rs 250", text)

        same = self._ship(oid, awb=DTDC_AWB.lower(), courier="dtdc")
        self.assertEqual((same.status_code, same.get_json()["changed"]), (200, False))
        other = self._ship(oid, awb="7X119057820")
        self.assertEqual((other.status_code, other.get_json()["error"]), (409, "forward_shipment_exists"))
        self.assertEqual(self._ship(oid, shipment_type="REPAIRED_ORIGINAL").get_json()["error"],
                         "forward_shipment_exists")
        self.assertEqual(len(self.mails), 1)
        events = self._events(oid)
        self.assertEqual(events.count("reverse_pickup.forward_shipped"), 1)
        ev = [json.loads(o["body"]) for o in self._outbox(oid)
              if o["event"] == "reverse_pickup.forward_shipped"][0]
        self.assertEqual((ev["data"]["shipment_type"], ev["data"]["awb"], ev["data"]["original_awb"]),
                         ("REPLACEMENT", DTDC_AWB, trp.FORWARD))
        self.assertEqual([h for h in self._history(oid) if "shipped to customer" in h],
                         ["Return: REPLACEMENT shipped to customer via DTDC, AWB %s (original AWB %s "
                          "unchanged) by ops-api-token:dispatch" % (DTDC_AWB, trp.FORWARD)])
        self.cur.execute("SELECT tracking_number FROM ops_shipping_awb WHERE ow_order_id=%s ORDER BY id",
                         (oid,))
        self.assertEqual([r["tracking_number"] for r in self.cur.fetchall()], [trp.FORWARD, DTDC_AWB])

        # the same AWB cannot be the shipment of a second return
        oid2, _ = self._inspected(defect=True, fee="WAIVED")
        self.assertEqual(self._ship(oid2).get_json()["error"], "awb_in_use")

    def test_the_original_goes_back_only_after_consent_and_a_defect_ships_no_original(self):
        oid, _ = self._inspected(defect=False)
        r = self._ship(oid, shipment_type="REPLACEMENT")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "shipment_type_mismatch"))
        r = self._ship(oid, shipment_type="ORIGINAL_RETURNED")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "consent_required"))
        self._post(oid, {"message_id": "<ok@mail>"}, path="/consent")
        self.assertEqual(self._get("/ops/api/shipments/%s/reverse-pickup" % oid).get_json()["queue_state"],
                         "READY_TO_DISPATCH")
        r = self._ship(oid, shipment_type="ORIGINAL_RETURNED", courier="Delhivery", awb="3612052999999")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["queue_state"], "FORWARD_SHIPPED")
        self.assertIn("As you requested, your product is on its way back to you.", self.mails[0][2])
        self.assertIn("https://www.delhivery.com/track-v2/package/3612052999999", self.mails[0][2])

        not_inspected, awb = self._booked("WAIVED")
        self._received(not_inspected, awb)
        self.assertEqual(self._ship(not_inspected).get_json()["error"], "not_inspected")

    def test_completion_is_explicit_matches_the_shipment_and_happens_once(self):
        oid, _ = self._inspected(defect=True, fee="WAIVED")
        self.assertEqual(self._complete(oid, "DONE").get_json()["error"], "invalid_outcome")
        r = self._complete(oid, "REPLACEMENT_SHIPPED")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "forward_shipment_required"))
        r = self._complete(oid, "PRODUCT_REFUNDED")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "product_refund_not_executed"))
        self._ship(oid, shipment_type="REPAIRED_ORIGINAL")
        del self.mails[:]
        self.assertEqual(self._complete(oid, "REPLACEMENT_SHIPPED").get_json()["error"], "outcome_mismatch")
        self.assertEqual(self._complete(oid, "PRODUCT_REFUNDED").get_json()["error"], "outcome_mismatch")
        state = self._get("/ops/api/shipments/%s/reverse-pickup" % oid).get_json()
        self.assertIsNone(state["completion"])
        self.assertEqual(state["queue_state"], "FORWARD_SHIPPED")

        r = self._complete(oid, "REPAIRED_ORIGINAL_SHIPPED", note="customer confirmed receipt")
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertEqual((j["changed"], j["queue_state"]), (True, "COMPLETED"))
        self.assertEqual((j["completion"]["outcome"], j["completion"]["note"], j["completion"]["completed_by"]),
                         ("REPAIRED_ORIGINAL_SHIPPED", "customer confirmed receipt", "ops-api-token:ops-lead"))
        self.assertTrue(j["completion"]["completed_at"])
        self.assertEqual([m[1] for m in self.mails], ["Optiwar Return Completed"])
        self.assertIn("Your repaired product was shipped back to you (DTDC AWB %s)." % DTDC_AWB,
                      self.mails[0][2])

        same = self._complete(oid, "REPAIRED_ORIGINAL_SHIPPED")
        self.assertEqual((same.status_code, same.get_json()["changed"]), (200, False))
        flip = self._complete(oid, "REPLACEMENT_SHIPPED")
        self.assertEqual((flip.status_code, flip.get_json()["error"]), (409, "completed_exists"))
        self.assertEqual(self._ship(oid, awb="7X119057821").get_json()["error"], "case_completed")
        self.assertEqual(len(self.mails), 1)
        self.assertEqual(self._events(oid).count("reverse_pickup.completed"), 1)
        ev = [json.loads(o["body"]) for o in self._outbox(oid) if o["event"] == "reverse_pickup.completed"][0]
        self.assertEqual((ev["data"]["outcome"], ev["data"]["forward_awb"], ev["data"]["fee_state"]),
                         ("REPAIRED_ORIGINAL_SHIPPED", DTDC_AWB, "WAIVED"))
        self.assertTrue(any(h.startswith("Return completed: REPAIRED_ORIGINAL_SHIPPED by ")
                            and h.endswith(": customer confirmed receipt") for h in self._history(oid)))
        # the queue shows it as COMPLETED, and the case allows a fresh request
        self.assertTrue(rreq._case_allows_request(rp.case_for_order(self.db, oid)))

    def test_a_product_refund_completes_a_confirmed_defect_without_a_shipment(self):
        oid, _ = self._inspected(defect=False)
        self._post(oid, {"message_id": "<ok@mail>"}, path="/consent")
        r = self._complete(oid, "PRODUCT_REFUNDED")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "defect_not_confirmed"))

        oid, _ = self._inspected(defect=True, fee="WAIVED")
        self.assertEqual(self._complete(oid, "PRODUCT_REFUNDED").get_json()["error"],
                         "product_refund_not_executed")
        # an assessment recorded but not executed is not a refund
        self.cur.execute(
            "INSERT INTO return_assessments (order_id, site, claim_type, currency, paid_minor, "
            "lens_deduction_cap_pct, proposed_refund_minor, assessed_by, service_identity, assessed_at) "
            "VALUES (%s,'in','MANUFACTURING_DEFECT','INR',94900,50,94900,'ops','in-ops',NOW())", (oid,))
        self.db.commit()
        aid = self.cur.lastrowid
        self.assertEqual(self._complete(oid, "PRODUCT_REFUNDED").get_json()["error"],
                         "product_refund_not_executed")
        self.assertTrue(assess.mark_executed(self.cur, aid, 4242))
        self.db.commit()
        r = self._complete(oid, "PRODUCT_REFUNDED")
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertEqual((j["queue_state"], j["completion"]["outcome"], j["forward"]), ("COMPLETED", "PRODUCT_REFUNDED", None))
        self.assertIn("The product refund has been processed to your original payment method",
                      self.mails[0][2])
        self.assertNotIn("AWB", self.mails[0][2])
        # nothing ships after a refund, and no shipped outcome can be claimed
        self.assertEqual(self._ship(oid).get_json()["error"], "case_completed")
        self.assertEqual(self._complete(oid, "REPLACEMENT_SHIPPED").get_json()["error"], "completed_exists")

    def test_an_open_fee_refund_blocks_completion_and_the_paid_fee_path_is_unchanged(self):
        oid, _ = self._inspected(defect=True, fee="PAID")
        case = rp.case_for_order(self.db, oid)
        self.assertEqual(case["fee_refund_state"], rp.REFUND_PENDING)
        self._ship(oid)
        # the shipment is recorded while the fee refund is still pending ...
        self.assertEqual(self._get("/ops/api/shipments/%s/reverse-pickup" % oid).get_json()["queue_state"],
                         "REFUND_PENDING")
        r = self._complete(oid, "REPLACEMENT_SHIPPED")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "fee_refund_open"))
        # ... and completion follows once it is settled
        self.cur.execute("UPDATE reverse_pickup_cases SET fee_refund_state=%s, fee_state=%s, "
                         "fee_refunded_minor=25000, fee_refund_id='rfnd_x' WHERE id=%s",
                         (rp.REFUND_DONE, rp.FEE_REFUNDED, case["id"]))
        self.db.commit()
        self.assertEqual(self._get("/ops/api/shipments/%s/reverse-pickup" % oid).get_json()["queue_state"],
                         "FORWARD_SHIPPED")
        r = self._complete(oid, "REPLACEMENT_SHIPPED")
        self.assertEqual((r.status_code, r.get_json()["queue_state"]), (200, "COMPLETED"))

    def test_a_failed_shipment_notice_is_retried_and_never_sent_twice(self):
        oid, _ = self._inspected(defect=True, fee="WAIVED")

        def boom(to, subj, text):
            raise RuntimeError("smtp down")
        trp.reship._default_mailer = boom
        r = self._ship(oid)
        self.assertEqual((r.status_code, r.get_json()["customer_notice"]["result"]), (200, "failed"))
        trp.reship._default_mailer = lambda to, subj, text: self.mails.append((to, subj, text))
        self.assertEqual(self._ship(oid).get_json()["changed"], False)
        self.assertEqual(self.mails, [])
        self.assertEqual(rp.retry_notices(self.db)["due"], 0)
        self._age_notices(oid)
        self.assertEqual(rp.retry_notices(self.db)["sent"], 1)
        self._age_notices(oid, minutes=120)
        self.assertEqual(rp.retry_notices(self.db)["due"], 0)
        self.assertEqual([m[1] for m in self.mails], ["Optiwar Return: Shipment on Its Way to You"])

    def test_my_orders_follows_the_parcel_once_optiwar_has_it(self):
        oid, awb = self._booked()
        card = self._card(oid)
        self.assertEqual((card["reverse_pickup"]["state"], card["return_request"]), ("BOOKED", None))
        self.assertEqual(card["stage_label"], "Reverse pickup scheduled")
        self.assertEqual(self._received(oid, awb).status_code, 200)
        card = self._card(oid)
        self.assertEqual((card["return_request"]["state"], card["reverse_pickup"]), ("RECEIVED", None))
        self.assertEqual(card["stage_label"], "Return: parcel received")
        self._post(oid, {"operator": "qc", "manufacturing_defect": True}, path="/inspection")
        card = self._card(oid)
        rq = card["return_request"]
        self.assertEqual((rq["state"], rq["refund_pending"], rq["fee_waived"], rq["can_request"]),
                         ("DEFECT_CONFIRMED", True, False, False))
        self.assertEqual((card["reverse_pickup"], card["stage_label"]), (None, "Return: defect confirmed"))
        for key in ("inspected_by", "inspection_remarks", "received_by", "received_notes"):
            self.assertNotIn(key, rq)

        waived, _ = self._inspected(defect=True, fee="WAIVED")
        rq = self._card(waived)["return_request"]
        self.assertEqual((rq["state"], rq["refund_pending"], rq["fee_waived"]), ("DEFECT_CONFIRMED", False, True))

        kept, _ = self._inspected(defect=False)
        self._post(kept, {"message_id": "<ok@mail>"}, path="/consent")
        card = self._card(kept)
        self.assertEqual((card["return_request"]["state"], card["reverse_pickup"], card["stage_label"]),
                         ("SENDING_BACK", None, "Return: to be sent back"))

        self._ship(kept, shipment_type="ORIGINAL_RETURNED")
        self._complete(kept, "ORIGINAL_RETURNED")
        self.cur.execute("SELECT status FROM order_reverse_pickups WHERE order_id=%s", (kept,))
        self.assertEqual({r["status"] for r in self.cur.fetchall()}, {rp.ST_CLOSED})
        orders = [{"order_id": kept, "stage_label": "Delivered", "stage_tone": "shipped"}]
        customer_orders.attach_reverse_pickup(orders, rp.latest_for_customer(self.db, self._cid(kept)),
                                              request_cards={kept: {"state": "SUBMITTED"}})
        self.assertEqual((orders[0]["reverse_pickup"], orders[0]["stage_label"]), (None, "Return requested"))

    def test_my_orders_and_the_assistant_show_the_shipment_only_once_recorded(self):
        oid, _ = self._inspected(defect=True, fee="WAIVED")
        card = self._card(oid)
        self.assertFalse(card["return_request"]
                         and card["return_request"]["state"] in ("SHIPPED_TO_CUSTOMER", "COMPLETED"))
        e = ra._find(self._model(oid), oid)
        self.assertEqual((e["stage"], e["forward_awb"], e["completed_outcome"]), (ra.ST_DEFECT, None, None))
        m = self._model(oid)
        self.assertEqual(ra.reply_violations(m, "Your replacement AWB is %s." % DTDC_AWB), [ra.V_AWB_INVENTED])

        self._ship(oid)
        card = self._card(oid)
        rq = card["return_request"]
        self.assertEqual((rq["state"], rq["shipment"]["type"], rq["shipment"]["awb"], rq["can_request"]),
                         ("SHIPPED_TO_CUSTOMER", "REPLACEMENT", DTDC_AWB, False))
        self.assertEqual(rq["shipment"]["track_url"], "https://www.dtdc.com/track")
        self.assertIsNone(card["reverse_pickup"])
        self.assertEqual(card["stage_label"], "Return: shipped to you")
        m = self._model(oid)
        e = ra._find(m, oid)
        self.assertEqual((e["stage"], e["shipped_type"], e["forward_awb"], e["forward_courier"]),
                         (ra.ST_SHIPPED, "REPLACEMENT", DTDC_AWB, "DTDC"))
        self.assertEqual(ra.get_return_case_status(m, oid)["shipped_to_customer"]["awb"], DTDC_AWB)
        text = ra.prompt_section(m)
        self.assertIn("stage=SHIPPED_TO_CUSTOMER", text)
        self.assertIn("shipped=REPLACEMENT forward_awb=%s (DTDC)" % DTDC_AWB, text)
        self.assertEqual(ra.reply_violations(m, "Your replacement AWB is %s." % DTDC_AWB), [])
        self.assertEqual(ra.reply_violations(m, "Your replacement AWB is 7X119057899."), [ra.V_AWB_INVENTED])
        ctx = ra.ket_context(m)[0]
        self.assertEqual((ctx["stage"], ctx["forward_awb"], ctx["completed_outcome"]),
                         (ra.ST_SHIPPED, DTDC_AWB, None))

        self._complete(oid, "REPLACEMENT_SHIPPED")
        card = self._card(oid)
        rq = card["return_request"]
        self.assertEqual((rq["state"], rq["outcome"], rq["shipment"]["awb"], rq["can_request"]),
                         ("COMPLETED", "REPLACEMENT_SHIPPED", DTDC_AWB, False))
        self.assertEqual(card["stage_label"], "Return complete")
        m = self._model(oid)
        e = ra._find(m, oid)
        self.assertEqual((e["stage"], e["completed_outcome"]), (ra.ST_COMPLETED, "REPLACEMENT_SHIPPED"))
        self.assertIn("outcome=REPLACEMENT_SHIPPED", ra.prompt_section(m))
        self.assertEqual(ra.get_return_case_status(m, oid)["completed"], True)


del RPT  # borrowed above; not a test case of this module


if __name__ == "__main__":
    unittest.main()
