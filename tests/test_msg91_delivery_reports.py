"""Regression tests for MSG91 delivery-report parsing.

On 2026-08-12 MSG91 auto-paused the WhatsApp ``optiwar-delivery-report`` webhook
after our endpoint answered HTTP 400 to 62 consecutive callbacks: the handler
required a flat ``request_id``/``status`` object, which is what the SMS report
looks like, and the WhatsApp "On Outbound Report Received" report is an envelope
with the reports nested inside. Delivery truth stopped arriving.

These tests pin the two properties that failure taught us:

  - every shape the provider sends for the same event parses;
  - a shape we do not recognise is *acknowledged*, never refused — a non-2xx
    answer costs the whole webhook, not just the report we could not read.

Runs without a Flask app: ``flask`` and ``flaskr.*`` are stubbed so crm.py
imports in isolation.

    python3 -m unittest tests.test_msg91_delivery_reports
"""
import importlib.util
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _install_stubs():
    flask_mod = types.ModuleType("flask")

    class _Blueprint:
        def __init__(self, *a, **k):
            pass

        def route(self, *a, **k):
            return lambda fn: fn

    flask_mod.Blueprint = _Blueprint
    flask_mod.Flask = object
    flask_mod.current_app = types.SimpleNamespace(config={})
    flask_mod.request = types.SimpleNamespace(remote_addr="127.0.0.1")
    for name in ("render_template", "url_for", "flash", "redirect", "session",
                 "jsonify"):
        setattr(flask_mod, name, lambda *a, **k: None)
    sys.modules["flask"] = flask_mod

    mail_stub = types.ModuleType("flask_mail")
    mail_stub.Message = object
    sys.modules["flask_mail"] = mail_stub

    pkg = types.ModuleType("flaskr")
    pkg.__path__ = [REPO]
    sys.modules["flaskr"] = pkg
    for name, attrs in (("flaskr.db", {"get_db": lambda *a, **k: None}),
                        ("flaskr.mail", {"send_contact_email": lambda *a, **k: None,
                                         "create_ticket_in_db": lambda *a, **k: None}),
                        ("flaskr.captcha", {"CaptchaGenerator": object})):
        mod = types.ModuleType(name)
        for attr, val in attrs.items():
            setattr(mod, attr, val)
        sys.modules[name] = mod


def _load_crm():
    _install_stubs()
    spec = importlib.util.spec_from_file_location(
        "flaskr.crm_under_test", os.path.join(REPO, "crm.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["flaskr.crm_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


_SAVED = {}
_TOUCHED = ("flask", "flask_mail", "flaskr", "flaskr.db", "flaskr.mail",
            "flaskr.captcha", "flaskr.crm_under_test")
parse = None


def setUpModule():
    # Stubbing is global, so snapshot what we displace: other suites in this
    # run stub the same names differently and must not inherit ours.
    for name in _TOUCHED:
        _SAVED[name] = sys.modules.get(name)
    global parse
    parse = _load_crm().parse_msg91_reports


def tearDownModule():
    for name, mod in _SAVED.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


class SmsShapeTests(unittest.TestCase):
    """The shape that already worked must keep working."""

    def test_flat_snake_case(self):
        self.assertEqual(
            parse({"request_id": "abc", "status": "delivered",
                   "failure_reason": "", "timestamp": "2026-08-11T16:39:28"}),
            [{"request_id": "abc", "status": "delivered",
              "failure_reason": "", "provider_ts": "2026-08-11T16:39:28"}])

    def test_flat_camel_case(self):
        got = parse({"requestId": "abc", "eventName": "READ",
                     "ts": "2026-08-11T16:39:28"})
        self.assertEqual(got[0]["request_id"], "abc")
        self.assertEqual(got[0]["status"], "read")


class WhatsAppShapeTests(unittest.TestCase):
    def test_envelope_with_a_messages_list(self):
        got = parse({
            "integrated_number": "919999999999",
            "content_type": "template",
            "messages": [
                {"id": "wamid.1", "status": "delivered",
                 "timestamp": "2026-08-12T07:48:00Z"},
                {"id": "wamid.2", "status": "failed", "error": "undeliverable"},
            ],
        })
        self.assertEqual([r["request_id"] for r in got], ["wamid.1", "wamid.2"])
        self.assertEqual([r["status"] for r in got], ["delivered", "failed"])
        self.assertEqual(got[1]["failure_reason"], "undeliverable")

    def test_request_id_on_the_envelope_and_status_on_the_child(self):
        # The id belongs to the send; the status belongs to the event.
        got = parse({"requestId": "req-1",
                     "reports": [{"status": "sent"}, {"status": "read"}]})
        self.assertEqual([(r["request_id"], r["status"]) for r in got],
                         [("req-1", "sent"), ("req-1", "read")])

    def test_bare_list_of_reports(self):
        got = parse([{"request_id": "a", "status": "sent"},
                     {"request_id": "b", "status": "read"}])
        self.assertEqual([r["request_id"] for r in got], ["a", "b"])

    def test_nested_data_envelope(self):
        got = parse({"data": {"messages": [{"msgId": "m1", "eventName": "SENT"}]}})
        self.assertEqual(got, [{"request_id": "m1", "status": "sent",
                                "failure_reason": "", "provider_ts": ""}])


class UnrecognisedShapeTests(unittest.TestCase):
    def test_a_report_without_an_id_or_status_is_not_invented(self):
        self.assertEqual(parse({"messages": [{"from": "9199", "text": "hi"}]}), [])
        self.assertEqual(parse({"request_id": "abc"}), [])
        self.assertEqual(parse({"status": "delivered"}), [])

    def test_non_report_bodies(self):
        for body in (None, "", "delivered", 7, [], {}, [[]]):
            self.assertEqual(parse(body), [], repr(body))

    def test_recursion_is_bounded(self):
        deep = {"data": {"data": {"data": {"data": {"data": {
            "request_id": "x", "status": "sent"}}}}}}
        self.assertEqual(parse(deep), [])  # too deep to trust, not a crash


class FieldSafetyTests(unittest.TestCase):
    def test_oversized_values_are_truncated_to_their_columns(self):
        got = parse({"request_id": "r" * 400, "status": "s" * 80,
                     "reason": "x" * 400, "timestamp": "t" * 200})[0]
        self.assertEqual(len(got["request_id"]), 191)
        self.assertEqual(len(got["status"]), 32)
        self.assertEqual(len(got["failure_reason"]), 250)
        self.assertEqual(len(got["provider_ts"]), 64)

    def test_status_is_normalised_for_the_outbox_fold(self):
        # _store_delivery_event compares against lowercase names.
        self.assertEqual(parse({"id": "a", "status": " Delivered "})[0]["status"],
                         "delivered")


if __name__ == "__main__":
    unittest.main()
