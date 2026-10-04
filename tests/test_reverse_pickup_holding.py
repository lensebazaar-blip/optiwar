"""Reverse pickup phase 4b: after an inspection that did not confirm the
defect, the product is held for the customer's reply for 60 days from the
inspection, with reminders on days 30, 45 and 55, then the case is closed as
ABANDONED. Also the three review findings on phase 4a (repeat returns)."""
import datetime
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tests import test_reverse_pickup as trp  # noqa: E402
from tests import test_reverse_pickup_phase4 as tp4  # noqa: E402

rp = trp.rp
reship = trp.reship
ra = tp4.ra
rreq = tp4.rreq

P4 = tp4.Phase4Test


class HoldingTest(unittest.TestCase):
    setUpClass = classmethod(P4.setUpClass.__func__)
    tearDownClass = classmethod(P4.tearDownClass.__func__)
    setUp, tearDown = P4.setUp, P4.tearDown
    _awb_seq = P4._awb_seq
    (_order, _case, _post, _body, _history, _get, _outbox, _booked, _received, _events, _age_notices,
     _inspected, _ship, _complete, _cid, _card, _model) = (
        P4._order, P4._case, P4._post, P4._body, P4._history, P4._get, P4._outbox, P4._booked,
        P4._received, P4._events, P4._age_notices, P4._inspected, P4._ship, P4._complete, P4._cid,
        P4._card, P4._model)

    # ----------------------------------------------------------- helpers

    def _row(self, oid):
        self.cur.execute("SELECT * FROM reverse_pickup_cases WHERE order_id=%s ORDER BY case_no DESC LIMIT 1",
                         (oid,))
        row = self.cur.fetchone()
        self.db.commit()
        return row

    def _held(self, days):
        """A no-defect case inspected ``days`` days ago (by the DB clock)."""
        oid, awb = self._inspected(defect=False)
        self._age(oid, days)
        return oid, awb

    def _age(self, oid, days):
        self.cur.execute("UPDATE reverse_pickup_cases SET inspected_at=inspected_at - INTERVAL %s DAY, "
                         "abandon_at=abandon_at - INTERVAL %s DAY WHERE order_id=%s", (days, days, oid))
        self.db.commit()

    def _sweep(self, **kw):
        return rp.sweep_holding(self.db, **kw)

    def _subjects(self):
        return [m[1] for m in self.mails]

    # ------------------------------------------------------------- tests

    def test_the_hold_starts_at_a_no_defect_inspection_only(self):
        oid, awb = self._booked()
        self._received(oid, awb)
        self.assertIsNone(self._row(oid)["abandon_at"])
        self.assertIsNone(rp.hold_view(self._row(oid)))
        r = self._post(oid, {"operator": "qc", "manufacturing_defect": False,
                             "inspected_at": "2026-10-01T10:00:00+05:30"}, path="/inspection")
        self.assertEqual(r.status_code, 200, r.get_json())
        row = self._row(oid)
        self.assertEqual((row["abandon_at"] - row["inspected_at"]).days, 60)
        self.assertEqual(r.get_json()["hold"]["holding_days"], 60)
        self.assertEqual(r.get_json()["hold"]["reminder_days"], [30, 45, 55])
        text = [m[2] for m in self.mails if m[1] == "Optiwar Return Inspection Update"][0]
        self.assertIn("Please reply by %s (60 days from the inspection)" % row["abandon_at"].strftime("%d %b %Y"),
                      text)
        self.assertIn("treated as unclaimed under section 12", text)

        defect, _a = self._inspected(defect=True, fee="WAIVED")
        self.assertIsNone(self._row(defect)["abandon_at"])
        self.assertIsNone(rp.hold_view(self._row(defect)))

    def test_reminders_go_out_on_days_30_45_and_55_once_each(self):
        oid, _awb = self._held(29)
        self.assertEqual(self._sweep()["reminded"], 0)
        self.assertEqual(self.mails, [])
        self._age(oid, 1)
        s = self._sweep()
        self.assertEqual((s["open"], s["reminded"], s["abandoned"]), (1, 1, 0))
        self.assertEqual(self._subjects(), ["Optiwar Return: Your Reply Is Needed"])
        row = self._row(oid)
        self.assertIn("please reply to this email by %s (30 days remaining)"
                      % row["abandon_at"].strftime("%d %b %Y"), self.mails[0][2])
        self.assertIn("There is nothing to pay for that shipment.", self.mails[0][2])
        self.assertNotIn("Rs 250", self.mails[0][2])
        self.assertEqual(self._sweep()["reminded"], 0)
        self._age(oid, 14)
        self.assertEqual(self._sweep()["reminded"], 0)
        self._age(oid, 1)
        self.assertEqual(self._sweep()["reminded"], 1)
        self._age(oid, 10)
        self.assertEqual(self._sweep()["reminded"], 1)
        self.assertEqual(self._subjects(), ["Optiwar Return: Your Reply Is Needed"] * 2 + [
            "Optiwar Return: Final Notice Before the Product Is Treated as Unclaimed"])
        self.assertIn("(5 days remaining)", self.mails[2][2])
        events = [e["event_type"] for e in reship.events_for(self.db, oid)]
        self.assertEqual(events.count(rp.EV_HOLD_REMINDER), 2)
        self.assertEqual(events.count(rp.EV_HOLD_FINAL_WARNING), 1)
        ops = [o["event"] for o in self._outbox(oid)]
        self.assertEqual((ops.count(rp.EV_HOLD_REMINDER), ops.count(rp.EV_HOLD_FINAL_WARNING)), (2, 1))
        q = self._get("/ops/api/shipments/%s/reverse-pickup" % oid).get_json()
        self.assertEqual(q["queue_state"], "ABANDONMENT_PENDING")
        self.assertEqual(q["hold"]["days_remaining"], 5)
        self.assertEqual(self._row(oid)["completed_at"], None)

    def test_a_sweep_that_was_down_sends_only_the_latest_reminder(self):
        oid, _awb = self._held(56)
        self.assertEqual(self._sweep()["reminded"], 1)
        self.assertEqual(self._subjects(),
                         ["Optiwar Return: Final Notice Before the Product Is Treated as Unclaimed"])
        sent = [json.loads(e["payload"])["sent"] for e in reship.events_for(self.db, oid)
                if e["event_type"] in (rp.EV_HOLD_REMINDER, rp.EV_HOLD_FINAL_WARNING)]
        self.assertEqual(sent, [False, False, True])

    def test_day_60_closes_the_case_as_abandoned_once(self):
        oid, _awb = self._held(59)
        self._sweep()
        del self.mails[:]
        self.assertEqual(self._sweep()["abandoned"], 0)
        self._age(oid, 1)
        s = self._sweep()
        self.assertEqual(s["abandoned"], 1)
        row = self._row(oid)
        self.assertEqual((row["completed_outcome"], row["completed_by"]), ("ABANDONED", rp.ABANDON_OPERATOR))
        self.assertIsNotNone(row["abandoned_at"])
        self.assertIn("No customer reply within 60 days", row["abandon_reason"])
        self.assertEqual(self._subjects(), ["Optiwar Return Closed: Product Unclaimed"])
        self.assertIn("treated as unclaimed under section 12", self.mails[0][2])
        self.assertEqual(self._sweep()["abandoned"], 0)
        self.assertEqual(rp.abandon(self.db, row["case_uuid"])[0], "not_open")
        self.assertEqual(len(self.mails), 1)
        self.assertEqual([o["event"] for o in self._outbox(oid)].count(rp.EV_ABANDONED), 1)
        self.assertEqual([h for h in self._history(oid) if "ABANDONED" in h][:1] != [], True)
        self.cur.execute("SELECT status FROM order_reverse_pickups WHERE order_id=%s", (oid,))
        self.assertEqual({r["status"] for r in self.cur.fetchall()}, {rp.ST_CLOSED})
        self.db.commit()

        q = self._get("/ops/api/shipments/%s/reverse-pickup" % oid).get_json()
        self.assertEqual(q["queue_state"], "ABANDONED")
        mine = [c for c in rp.ops_queue(self.db) if c["order_id"] == oid]
        self.assertTrue(not mine or mine[0]["queue_state"] == "ABANDONED")

        card = self._card(oid)
        self.assertEqual(card["return_request"]["state"], "ABANDONED")
        self.assertFalse(card["return_request"]["can_request"])
        self.assertIsNone(card["reverse_pickup"])
        self.assertEqual(card["stage_label"], "Return closed: unclaimed")

        m = self._model(oid)
        self.assertEqual(m["orders"][0]["stage"], ra.ST_ABANDONED)
        ctx = ra.ket_context(m)[0]
        self.assertEqual(ctx["completed_outcome"], "ABANDONED")
        self.assertIsNotNone(ctx["abandoned_at"])
        self.assertIsNone(ctx["reply_by"])

        late = self._post(oid, {"message_id": "<late@mail>"}, path="/consent")
        self.assertEqual((late.status_code, late.get_json()["error"]), (409, "case_abandoned"))

    def test_the_customer_and_the_assistant_see_the_deadline_while_it_runs(self):
        oid, _awb = self._held(10)
        row = self._row(oid)
        card = self._card(oid)
        rq = card["return_request"]
        self.assertEqual(rq["state"], "AWAITING_REPLY")
        self.assertEqual(rq["hold"]["days_remaining"], 50)
        self.assertFalse(rq["hold"]["final_period"])
        self.assertTrue(rq["fee_kept"])
        self.assertEqual(card["stage_label"], "Return: your reply needed")
        m = self._model(oid)
        e = m["orders"][0]
        self.assertEqual(e["stage"], ra.ST_NO_DEFECT)
        self.assertEqual((e["reply_by"], e["reply_days_remaining"]), (row["abandon_at"], 50))
        self.assertEqual(ra.ket_context(m)[0]["reply_by"], ra._fmt(row["abandon_at"]))
        st = ra.get_return_case_status(m, oid)
        self.assertEqual((st["reply_days_remaining"], st["abandoned"]), (50, False))

    def test_a_reply_a_shipment_or_a_defect_is_never_abandoned(self):
        replied, _a = self._held(61)
        r = self._post(replied, {"message_id": "<r@mail>"}, path="/consent")
        self.assertEqual(r.status_code, 200, r.get_json())
        shipped, _b = self._held(70)
        self._post(shipped, {"message_id": "<s@mail>"}, path="/consent")
        r = self._ship(shipped, shipment_type="ORIGINAL_RETURNED")
        self.assertEqual(r.status_code, 200, r.get_json())
        defects = []
        for state in ("PENDING", "FAILED", "EXCEPTION", "REFUNDED"):
            oid, _c = self._inspected(defect=True)
            self.cur.execute("UPDATE reverse_pickup_cases SET fee_refund_state=%s, "
                             "inspected_at=inspected_at - INTERVAL 90 DAY WHERE order_id=%s", (state, oid))
            defects.append(oid)
        self.db.commit()
        del self.mails[:]
        s = self._sweep()
        self.assertEqual((s["reminded"], s["abandoned"]), (0, 0))
        self.assertEqual(self.mails, [])
        for oid in [replied, shipped] + defects:
            self.assertNotEqual(self._row(oid)["completed_outcome"], "ABANDONED", oid)
        self.assertEqual(self._card(replied)["return_request"]["state"], "SENDING_BACK")

    def test_a_reply_recorded_before_the_lock_wins(self):
        oid, _awb = self._held(61)
        row = self._row(oid)
        self.cur.execute("UPDATE reverse_pickup_cases SET consent_at=NOW() WHERE id=%s", (row["id"],))
        self.db.commit()
        self.assertEqual(rp.abandon(self.db, row["case_uuid"])[0], "not_open")
        self.assertIsNone(self._row(oid)["completed_at"])
        fresh, _b = self._held(30)
        self.assertEqual(rp.abandon(self.db, self._row(fresh)["case_uuid"])[0], "not_due")

    def test_an_injected_clock_drives_the_sweep(self):
        oid, _awb = self._held(0)
        row = self._row(oid)
        s = self._sweep(now=row["abandon_at"])
        self.assertEqual(s["abandoned"], 1)
        self.assertEqual(self._row(oid)["completed_outcome"], "ABANDONED")

    def test_a_failed_reminder_email_is_retried(self):
        oid, _awb = self._held(30)
        reship._default_mailer = lambda *a: (_ for _ in ()).throw(RuntimeError("smtp down"))
        self.assertEqual(self._sweep()["reminded"], 1)
        self.cur.execute("SELECT notification_type, status FROM reverse_pickup_notifications WHERE order_id=%s "
                         "AND notification_type LIKE 'hold_%%'", (oid,))
        self.assertEqual([(r["notification_type"], r["status"]) for r in self.cur.fetchall()],
                         [("hold_reminder_day30", "FAILED")])
        self.db.commit()
        reship._default_mailer = lambda to, subj, text: self.mails.append((to, subj, text))
        self.assertEqual(self._sweep()["reminded"], 0)
        self._age_notices(oid)
        out = rp.retry_notices(self.db)
        self.assertGreaterEqual(out["sent"], 1)
        self.assertEqual(self._subjects(), ["Optiwar Return: Your Reply Is Needed"])

    def test_a_reminder_still_owed_after_the_reply_is_not_sent(self):
        oid, _awb = self._held(30)
        reship._default_mailer = lambda *a: (_ for _ in ()).throw(RuntimeError("smtp down"))
        self._sweep()
        reship._default_mailer = lambda to, subj, text: self.mails.append((to, subj, text))
        r = self._post(oid, {"message_id": "<r@mail>"}, path="/consent")
        self.assertEqual(r.status_code, 200, r.get_json())
        del self.mails[:]
        self._age_notices(oid)
        self.assertEqual(rp.retry_notices(self.db)["stale"], 1)
        self.assertEqual(self.mails, [])
        self.cur.execute("SELECT status FROM reverse_pickup_notifications WHERE order_id=%s "
                         "AND notification_type='hold_reminder_day30'", (oid,))
        self.assertEqual(self.cur.fetchone()["status"], "STALE")
        self.db.commit()

    def test_a_test_order_is_not_swept(self):
        oid, _awb = self._held(61)
        self.cur.execute("UPDATE orders SET is_test=1 WHERE order_id=%s", (oid,))
        self.db.commit()
        s = self._sweep()
        self.assertEqual(s["abandoned"], 0)
        self.assertGreaterEqual(s["skipped_test"], 1)
        self.assertIsNone(self._row(oid)["completed_at"])

    def test_an_inspection_recorded_before_4b_gets_its_deadline_from_the_inspection(self):
        oid, _awb = self._held(5)
        self.cur.execute("UPDATE reverse_pickup_cases SET abandon_at=NULL WHERE order_id=%s", (oid,))
        self.db.commit()
        rp._SCHEMA_READY = False
        rp.ensure_schema(self.db)
        row = self._row(oid)
        self.assertEqual((row["abandon_at"] - row["inspected_at"]).days, 60)

    # ------------------------------------------- review findings on #197

    def test_a_second_return_can_book_once_the_first_is_complete(self):
        oid, _awb = self._inspected(defect=True, fee="WAIVED")
        self.assertEqual(self._ship(oid).status_code, 200)
        r = self._complete(oid, "REPLACEMENT_SHIPPED")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.cur.execute("SELECT status FROM order_reverse_pickups WHERE order_id=%s", (oid,))
        self.assertEqual({p["status"] for p in self.cur.fetchall()}, {rp.ST_CLOSED})
        self.cur.execute("INSERT INTO reverse_pickup_cases (case_uuid, order_id, case_no, source, fee_state, "
                         "fee_amount_minor, fee_currency, created_by) VALUES (UUID(),%s,2,'test','WAIVED',"
                         "25000,'INR','test')", (oid,))
        self.db.commit()
        self._awb_seq[0] += 1
        r = self._post(oid, self._body(awb="3612052%07d" % self._awb_seq[0], forward_awb=tp4.DTDC_AWB))
        self.assertEqual(r.status_code, 200, r.get_json())
        state = self._get("/ops/api/shipments/%s/reverse-pickup" % oid).get_json()
        self.assertEqual(state["queue_state"], "PICKUP_BOOKED")
        self.assertEqual(state["forward_shipment"]["original"]["awb"], tp4.DTDC_AWB)

    def test_an_earlier_returns_refund_does_not_close_a_new_case(self):
        oid, _awb = self._inspected(defect=True, fee="WAIVED")
        self.cur.execute("INSERT INTO return_assessments (order_id, site, claim_type, currency, paid_minor, "
                         "lens_deduction_cap_pct, proposed_refund_minor, assessed_by, service_identity, "
                         "assessed_at, refund_id, executed_at) VALUES (%s,'in','DEFECT','INR',94900,50,94900,"
                         "'ops','t',NOW() - INTERVAL 30 DAY,1,NOW() - INTERVAL 30 DAY)", (oid,))
        self.cur.execute("UPDATE reverse_pickup_cases SET created_at=NOW() - INTERVAL 1 DAY WHERE order_id=%s",
                         (oid,))
        self.db.commit()
        r = self._complete(oid, "PRODUCT_REFUNDED")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "product_refund_not_executed"))
        self.cur.execute("INSERT INTO return_assessments (order_id, site, claim_type, currency, paid_minor, "
                         "lens_deduction_cap_pct, proposed_refund_minor, assessed_by, service_identity, "
                         "assessed_at, refund_id, executed_at) VALUES (%s,'in','DEFECT','INR',94900,50,94900,"
                         "'ops','t',NOW(),2,NOW())", (oid,))
        self.db.commit()
        self.assertEqual(self._complete(oid, "PRODUCT_REFUNDED").status_code, 200)

    def test_the_original_shipment_is_the_last_one_before_the_booking(self):
        _c, oid = self._order()
        self.cur.execute("UPDATE ops_shipping_awb SET created_at=NOW() - INTERVAL 20 DAY WHERE ow_order_id=%s",
                         (oid,))
        self.cur.execute("INSERT INTO ops_shipping_awb (ow_order_id, tracking_number, courier, awb_status, "
                         "created_by, created_at) VALUES (%s,'7X100000001','DTDC','created','ops',"
                         "NOW() - INTERVAL 10 DAY)", (oid,))
        self.cur.execute("INSERT INTO ops_shipping_awb (ow_order_id, tracking_number, courier, awb_status, "
                         "created_by, created_at) VALUES (%s,'7X100000002','DTDC','created','ops',NOW())", (oid,))
        self.db.commit()
        booked = reship.db_now(self.db).replace(microsecond=0)
        pickup = {"booked_at": booked - datetime.timedelta(days=1)}
        self.assertEqual(rp.original_shipment(self.db, oid, pickup), ("7X100000001", "DTDC"))
        self.assertEqual(rp.original_shipment(self.db, oid, None)[0], "7X100000002")


del P4  # borrowed harness only; its tests run in their own module


if __name__ == "__main__":
    unittest.main()
