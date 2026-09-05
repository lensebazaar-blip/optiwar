"""The payment reconciliation daily-report section, without a database.

The invariant it states — a payment captured at Razorpay must not be Pending
at Optiwar past the grace period — has to be RED whether the worker found it
or the application logged it, and the section has to render even when the
tables are unreadable and the worker has never run.
"""
import datetime
import importlib.util
import json
import os
import tempfile
import unittest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prs = _load("payment_report_section", "reports/payment_report_section.py")
report_executive = _load("report_executive", "reports/report_executive.py")

NOW = datetime.datetime(2026, 9, 6, 6, 0, 0)


def _metrics(**over):
    m = {"by_source": {"storefront": 3, "razorpay-webhook": 2},
         "half_applied": [],
         "tags": {tag: 0 for tag, _ in prs.LOG_TAGS},
         "worker": {"generated_at": "2026-09-06 05:55:00", "checked": 4, "settled": 0,
                    "unpaid": 4, "duplicate": 0, "exception": 0, "over_grace": 0,
                    "exceptions": []},
         "worker_stale": False, "worker_age": 5.0}
    m.update(over)
    return m


class StatusTest(unittest.TestCase):

    def test_quiet_day_is_green(self):
        self.assertEqual(prs.GREEN, prs.status(_metrics())[0])

    def test_invariant_from_the_worker_is_red(self):
        m = _metrics()
        m["worker"]["over_grace"] = 1
        v, why = prs.status(m)
        self.assertEqual(prs.RED, v)
        self.assertIn("grace", why)

    def test_invariant_from_the_log_is_red(self):
        m = _metrics()
        m["tags"]["PAYMENT_INVARIANT_RED"] = 1
        self.assertEqual(prs.RED, prs.status(m)[0])

    def test_reconciliation_exception_is_red(self):
        m = _metrics()
        m["worker"]["exceptions"] = [{"order_id": "A", "payment_id": "p", "detail": "x"}]
        self.assertEqual(prs.RED, prs.status(m)[0])

    def test_payment_without_processed_is_red(self):
        self.assertEqual(prs.RED, prs.status(_metrics(half_applied=["X-1"]))[0])

    def test_unmatched_webhook_is_amber(self):
        m = _metrics()
        m["tags"]["RAZORPAY_WEBHOOK_UNMATCHED"] = 1
        v, why = prs.status(m)
        self.assertEqual(prs.AMBER, v)
        self.assertIn("matched", why)

    def test_worker_not_running_is_amber_not_green(self):
        self.assertEqual(prs.AMBER, prs.status(_metrics(worker=None, worker_stale=True))[0])
        self.assertEqual(prs.AMBER, prs.status(_metrics(worker_stale=True))[0])

    def test_unreadable_tables_are_a_coverage_gap(self):
        self.assertEqual(prs.AMBER, prs.status(_metrics(by_source=None))[0])


class LogAndStateTest(unittest.TestCase):

    def test_tags_are_counted_inside_the_window_only(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write("[2026-09-05 15:55:00] ERROR in models: ACTIVITY:RAZORPAY_WEBHOOK_UNMATCHED "
                     "event:payment.captured payment:pay_1\n")
            fh.write("[2026-09-03 15:55:00] ERROR in models: ACTIVITY:RAZORPAY_WEBHOOK_UNMATCHED "
                     "event:payment.captured payment:pay_old\n")
            fh.write("[2026-09-05 16:00:00] INFO in models: ACTIVITY:RAZORPAY_DUPLICATE_SUPPRESSED "
                     "order:A payment:pay_1\n")
            fh.write("[2026-09-05 16:01:00] INFO in models: ACTIVITY:PRODUCT_VIEW product:1\n")
            path = fh.name
        try:
            counts = prs.tag_counts([path], now=NOW)
        finally:
            os.unlink(path)
        self.assertEqual(1, counts["RAZORPAY_WEBHOOK_UNMATCHED"])
        self.assertEqual(1, counts["RAZORPAY_DUPLICATE_SUPPRESSED"])
        self.assertEqual(0, counts["PAYMENT_INVARIANT_RED"])

    def test_missing_state_file_is_stale(self):
        data, stale, age = prs.worker_state("/nonexistent/razorpay.json", now=NOW)
        self.assertIsNone(data)
        self.assertTrue(stale)

    def test_old_state_file_is_stale_fresh_one_is_not(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"generated_at": "2026-09-06 05:50:00", "checked": 1}, fh)
            path = fh.name
        try:
            data, stale, age = prs.worker_state(path, now=NOW)
            self.assertFalse(stale)
            self.assertEqual(1, data["checked"])
            _, stale_old, _ = prs.worker_state(path, now=NOW + datetime.timedelta(hours=3))
            self.assertTrue(stale_old)
        finally:
            os.unlink(path)


class RenderTest(unittest.TestCase):

    def setUp(self):
        prs._reset_cache()

    def tearDown(self):
        prs._reset_cache()

    def test_section_renders_with_no_database_and_states_the_invariant(self):
        prs._CACHE.append((_metrics(by_source=None, worker=None, worker_stale=True),
                           ["by_source: no db"]))
        text = prs.build()
        self.assertIn("RAZORPAY PAYMENT RECONCILIATION", text)
        self.assertIn("STATUS: AMBER", text)
        self.assertIn("INVARIANT  captured at Razorpay + Pending locally > grace: 0", text)
        self.assertIn("NOT RUNNING", text)
        self.assertIn("[degraded] by_source: no db", text)

    def test_red_day_renders_red_and_emits_critical(self):
        m = _metrics()
        m["worker"]["over_grace"] = 1
        m["worker"]["settled"] = 1
        prs._CACHE.append((m, []))
        text = prs.build()
        self.assertIn("STATUS: RED", text)
        self.assertIn("[RED]", text)
        f = prs.findings()
        self.assertEqual(1, len(f))
        self.assertEqual("CRITICAL", f[0].severity)

    def test_every_owner_counter_is_on_the_page(self):
        prs._CACHE.append((_metrics(), []))
        text = prs.build()
        for label in ("Razorpay payments captured", "Matched by webhook",
                      "Matched by browser callback", "Recovered by order_id -> receipt lookup",
                      "Recovered by reconciliation worker", "RAZORPAY_WEBHOOK_UNMATCHED",
                      "Amount mismatch", "Currency mismatch", "Duplicate event suppressed",
                      "captured at Razorpay + Pending locally"):
            self.assertIn(label, text, label)

    def test_payments_is_an_expected_executive_source(self):
        self.assertIn("payments", report_executive.EXPECTED_SOURCES)


if __name__ == "__main__":
    unittest.main()
