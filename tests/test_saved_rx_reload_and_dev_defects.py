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
        # The summary travels to the browser, which says "Loaded" only after
        # reading the cards back (see test_saved_rx_apply_engine).
        self.assertIn('data-summary="{{ saved_summary }}"', cards)
        self.assertIn("'Loaded your saved prescription (' + (meta.summary || '')",
                      cards)
        self.assertNotIn("saved_loaded.summary", cards)
        models = _read("models.py")
        self.assertIn("saved_summary=lens_rx.saved_summary(saved) if saved "
                      "else ''", models)

    def test_the_server_prefill_wins_over_the_browser_form_memory(self):
        cards = _read("templates/_lens_eye_cards.html")
        self.assertIn('id="owLensForm" class="ow-rx" autocomplete="off"', cards)
        self.assertIn("function applyWanted()", cards)
        # RULES-mode lenses (Precision1) render their selectors server-side
        # and never went through fill(); the values are applied explicitly.
        rules_path = cards[cards.index("function refreshBc()"):][:400]
        self.assertIn("applyWanted();\n        update();", rules_path)

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


class SavedRxApplyEngine(unittest.TestCase):
    """The production defect: "Loaded your saved prescription (R -0.50 ·
    L -0.50)" over cards still holding -1.50 / -1.50. The saved values were
    fetched and the note rendered by the server, but "Use" was a link to
    ?saved=<id>#owLensForm: with the same id already in the URL the browser
    performs a fragment-only navigation, nothing is reloaded, and the cards
    keep whatever the customer had selected. One apply function now owns
    every entry method and says "Loaded" only after reading the cards back.

    The interactive proof (20 Use cycles over an active prescription, edit,
    reset, desktop + iPhone, both / right / left eyes) is
    tests/browser/saved_rx_apply_harness.py; these tests pin the wiring."""

    def setUp(self):
        self.cards = _read("templates/_lens_eye_cards.html")
        self.js = self.cards[self.cards.index("function applyPrescription("):]
        self.apply = self.js[:self.js.index("window.owApplyPrescription")]

    def test_saved_rx_replaces_existing_active_prescription(self):
        # Use is an in-page action on the current DOM, never a navigation the
        # browser can satisfy from the fragment.
        self.assertNotIn("saved={{ entry.cl_rx_id }}", self.cards)
        self.assertNotIn("#owLensForm", self.cards)
        self.assertIn('<button type="button" class="ow-rx-use" '
                      'data-role="use-saved"', self.cards)
        self.assertIn("data-rx='{{ entry.eyes|tojson }}'", self.cards)
        # The engine sets the eye inclusion, every parameter, and recomputes
        # the summary, in that order, whatever was there before.
        for step in ("includeBox(eye).checked = eyes.indexOf(eye) >= 0",
                     "applyIncluded();",
                     "eyes.forEach(function (eye) { cards[eye].set(rx[eye]); });",
                     "update();"):
            self.assertIn(step, self.apply)
        self.assertLess(self.apply.index("applyIncluded();"),
                        self.apply.index("cards[eye].set(rx[eye])"))
        self.assertLess(self.apply.index("cards[eye].set(rx[eye])"),
                        self.apply.index("update();"))
        # A card's set() replaces the whole wanted state: a parameter the
        # saved prescription does not state is cleared, not kept from before.
        card = self.cards[self.cards.index("set: function (values)"):][:600]
        self.assertIn("want = {};", card)
        self.assertIn("want[name] = (v === undefined || v === null) ? '' : String(v);",
                      card)
        # fill() must not let a stale previous selection win over an explicit
        # (possibly empty) wanted value.
        self.assertIn("(keep === undefined && entry.value === previous)",
                      self.cards)
        self.assertNotIn("(!keep && entry.value === previous)", self.cards)

    def test_saved_rx_can_be_reapplied_multiple_times(self):
        # Every entry method — the ?saved= bootstrap, the AI proposal, the
        # manual form and each Use click — is the same function, so the
        # second Use cannot differ from the first.
        self.assertEqual(self.cards.count("function applyPrescription("), 1)
        self.assertIn("applyPrescription(submittedRx(), 'saved', {", self.cards)
        self.assertIn("role(form, 'ai-note') ? 'ai' : 'manual'", self.cards)
        use = self.cards[self.cards.index('[data-role="use-saved"]\'), function (button)'):]
        self.assertIn("var ok = applyPrescription(rx, 'saved', {", use[:800])
        # Nothing is cached across clicks: the values come off the button's
        # own attribute each time and the state is read from the live DOM.
        self.assertIn("JSON.parse(button.getAttribute('data-rx')", use[:800])
        self.assertNotIn("pageshow", self.cards)
        state = self.cards[self.cards.index("window.owLensPageState = function"):][:200]
        self.assertIn("return {right: included('right'), left: included('left')", state)

    def test_saved_rx_success_requires_state_readback_match(self):
        # The note is hidden server-side; only the browser shows it, and only
        # after the read-back agrees with the requested prescription.
        self.assertIn('<p class="ow-rx-loaded" data-role="loaded-note" hidden></p>',
                      self.cards)
        self.assertNotIn("Loaded your saved prescription ({{", self.cards)
        readback = self.apply.index("var actual = cards[eye].read();")
        success = self.apply.index("'Loaded your saved prescription ('")
        failure = self.apply.index("if (mismatch.length) {")
        self.assertIn("'Saved prescription could not be applied. Please try again.'",
                      self.apply[failure:success])
        self.assertLess(readback, failure)
        self.assertLess(failure, success)
        self.assertIn("if (mismatch.length) {", self.apply)
        self.assertIn("if (!same(actual[name], wanted)) { mismatch.push(eye + '_' + name); }",
                      self.apply)
        # A failed apply leaves no reused_from claim on the form.
        self.assertEqual(self.apply.count("if (reused) { reused.value = ''; }"), 2)
        self.assertLess(self.apply.index("if (reused) { reused.value = ''; }"),
                        self.apply.index("if (reused) { reused.value = meta.clRxId || ''; }"))

    def test_saved_rx_defects_are_distinguished_and_carry_no_power(self):
        for code in ("RX_SAVED_FETCH_FAILED", "RX_SAVED_INCOMPATIBLE",
                     "RX_SAVED_APPLY_FAILED", "RX_SAVED_STATE_MISMATCH"):
            self.assertIn("defect('%s'" % code, self.cards)
        self.assertIn("defect('RX_SAVED_STATE_MISMATCH', 'post_apply_verification ' "
                      "+ mismatch.join(' '))", self.apply)
        self.assertIn("'product_id={{ lens.product_id }} '", self.cards)
        # The where-text names fields (right_sph), never values.
        self.assertNotIn("actual[name]", self.apply[self.apply.index("defect('RX_SAVED_STATE_MISMATCH'"):])
        app, log = DefectChannel._app(self)
        with app.test_client() as client:
            client.post("/api/chat/dev-defect", json={
                "code": "RX_SAVED_STATE_MISMATCH",
                "where": "product_id=1015 post_apply_verification right_sph -0.50",
                "page": "/categories/contact-lenses/precision1"})
        line = [rec for rec in log if "RX_SAVED_STATE_MISMATCH" in rec][0]
        self.assertIn("product_id=1015 post_apply_verification right_sph", line)
        self.assertNotIn("-0.50", line)
        self.assertNotIn("0.50", line)


if __name__ == "__main__":
    unittest.main()
