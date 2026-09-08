"""A saved prescription is loaded the same way every time, Ask AI opens the
chat, and a defect a customer meets reaches the development team's report.

Background: on production the first "Use" of a saved prescription filled the
cards; a reload showed "Loaded your saved prescription ()" with every power
back on "Select", and Ask AI did nothing. Three causes, each pinned here:
the loaded notice read a field only the list carried; the browser restored
the form's earlier state over the server's pre-fill; and the Ask AI button
clicked the chat orb, which only opens the Text/Voice menu.
"""
import datetime
import importlib.util
import logging
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _load(name, path=None):
    spec = importlib.util.spec_from_file_location(
        name, path or os.path.join(REPO, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


dev_defects = _load("dev_defects")
section = _load("dev_defects_section",
                os.path.join(REPO, "reports", "dev_defects_section.py"))


def _read(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as fh:
        return fh.read()


class SavedRxReload(unittest.TestCase):
    def test_the_loaded_notice_names_the_prescription(self):
        cards = _read("templates/_lens_eye_cards.html")
        self.assertIn("Loaded your saved prescription ({{ saved_summary }})",
                      cards)
        self.assertNotIn("saved_loaded.summary", cards)
        models = _read("models.py")
        self.assertIn("saved_summary=lens_rx.saved_summary(saved) if saved "
                      "else ''", models)

    def test_the_server_prefill_wins_over_the_browser_form_memory(self):
        cards = _read("templates/_lens_eye_cards.html")
        self.assertIn('id="owLensForm" class="ow-rx" autocomplete="off"', cards)
        self.assertIn("function applySubmitted()", cards)
        # RULES-mode lenses (Precision1) render their selectors server-side
        # and never went through fill(); the values are applied explicitly.
        rules_path = cards[cards.index("function refreshBc()"):][:400]
        self.assertIn("applySubmitted();\n        update();", rules_path)

    def test_ask_ai_opens_the_text_chat_itself(self):
        cards = _read("templates/_lens_eye_cards.html")
        self.assertIn("window.owChatOpen('text')", cards)
        self.assertNotIn("querySelector('.ow-chat-btn, .ow-login-btn')", cards)
        widget = _read("static/js/chat-widget.js")
        entry = widget[widget.index("window.owChatOpen = function"):][:400]
        self.assertIn("togglePanel(true)", entry)
        self.assertIn("choiceMenu.classList.remove('open')", entry)
        base = _read("templates/base.html")
        self.assertIn("chat-widget.js') + '?v=18'", base)


class DefectChannel(unittest.TestCase):
    def _app(self):
        from flask import Flask
        app = Flask("t")
        app.add_url_rule("/api/chat/dev-defect", "dev_defect",
                         dev_defects.browser_defect, methods=["POST"])
        records = []

        class H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        app.logger.addHandler(H())
        app.logger.setLevel(logging.INFO)
        return app, records

    def test_a_browser_defect_is_one_bounded_error_line(self):
        app, records = self._app()
        with app.test_client() as c:
            r = c.post("/api/chat/dev-defect", json={
                "code": "LENS_PAGE_JS_ERROR", "where": "x" * 500,
                "page": "/categories/contact-lenses/precision1"})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(len(records), 1)
        line = records[0]
        self.assertIn("ACTIVITY:DEV_DEFECT code=LENS_PAGE_JS_ERROR "
                      "origin=browser where=", line)
        self.assertIn("page=/categories/contact-lenses/precision1", line)
        self.assertLess(len(line), 400)

    def test_a_power_is_not_kept_even_if_the_browser_sends_one(self):
        app, records = self._app()
        with app.test_client() as c:
            c.post("/api/chat/dev-defect", json={
                "code": "CHAT_MESSAGE_NETWORK",
                "where": "right -3.75 left +1.25 bc 8.30", "page": "/p"})
        self.assertNotIn("-3.75", records[0])
        self.assertNotIn("+1.25", records[0])

    def test_an_unnamed_or_malformed_code_is_dropped(self):
        app, records = self._app()
        with app.test_client() as c:
            for body in ({"code": "drop table"}, {"code": ""}, {},
                         {"code": "x" * 80}):
                self.assertEqual(c.post("/api/chat/dev-defect",
                                        json=body).status_code, 204)
            self.assertEqual(c.post("/api/chat/dev-defect",
                                    data="not json").status_code, 204)
        self.assertEqual(records, [])

    def test_the_server_records_a_defect_without_a_request(self):
        app, records = self._app()
        with app.app_context():
            ok = dev_defects.record("CHAT_AI_REPLY_FAILED",
                                    where="timeout", page="/x")
        self.assertTrue(ok)
        self.assertIn("code=CHAT_AI_REPLY_FAILED origin=server where=timeout "
                      "page=/x", records[0])

    def test_the_gateway_reports_its_own_failures(self):
        gateway = _read("chat_gateway.py")
        self.assertIn("dev_defects.record('CHAT_LENS_CONTEXT_UNAVAILABLE'",
                      gateway)
        self.assertIn("dev_defects.record('CHAT_AI_REPLY_FAILED'", gateway)
        self.assertIn("@bp.route('/dev-defect', methods=['POST'])", gateway)
        deploy = _read("deploy/deploy.py")
        self.assertIn('"dev_defects.py"', deploy)


class ReportSection(unittest.TestCase):
    def _log(self, lines):
        fd, path = tempfile.mkstemp(suffix=".log")
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_defects_are_grouped_by_code_for_the_development_team(self):
        now = datetime.datetime(2026, 9, 8, 9, 0, 0)
        path = self._log([
            "[2026-09-08 08:10:00,000] ERROR in dev_defects: ACTIVITY:DEV_DEFECT "
            "code=LENS_PAGE_JS_ERROR origin=browser where=fill is not defined "
            "page=/categories/contact-lenses/precision1",
            "[2026-09-08 08:12:00,000] ERROR in dev_defects: ACTIVITY:DEV_DEFECT "
            "code=LENS_PAGE_JS_ERROR origin=browser where=fill is not defined "
            "page=/categories/contact-lenses/precision1",
            "[2026-09-08 08:30:00,000] ERROR in dev_defects: ACTIVITY:DEV_DEFECT "
            "code=CHAT_AI_REPLY_FAILED origin=server where=timeout page=/p",
            # Outside the 24h window: yesterday's report had it.
            "[2026-09-06 08:30:00,000] ERROR in dev_defects: ACTIVITY:DEV_DEFECT "
            "code=OLD_ONE origin=server where=x page=/p",
            "[2026-09-08 08:31:00,000] INFO in models: ACTIVITY:PRODUCT_VIEW "
            "product:96",
        ])
        groups = section.defects([path], now=now)
        self.assertEqual(sorted(groups), ["CHAT_AI_REPLY_FAILED",
                                          "LENS_PAGE_JS_ERROR"])
        self.assertEqual(groups["LENS_PAGE_JS_ERROR"]["count"], 2)
        self.assertEqual(groups["LENS_PAGE_JS_ERROR"]["origins"], {"browser"})
        text = section.build(groups)
        self.assertIn("STATUS: AMBER", text)
        self.assertIn("LENS_PAGE_JS_ERROR", text)
        self.assertIn("x2", text)
        self.assertIn("where: fill is not defined", text)
        self.assertIn("one task per code", text)
        found = section.findings(groups)
        self.assertEqual(len(found), 2)
        self.assertTrue(all(f.severity == "ACTION" for f in found))

    def test_a_quiet_day_is_green_with_no_findings(self):
        path = self._log(["[2026-09-08 08:31:00,000] INFO in models: x"])
        groups = section.defects([path])
        self.assertEqual(groups, {})
        self.assertIn("STATUS: GREEN", section.build(groups))
        self.assertEqual(section.findings(groups), [])


if __name__ == "__main__":
    unittest.main()
