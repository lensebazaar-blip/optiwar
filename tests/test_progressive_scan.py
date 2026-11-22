import os
import re
import unittest

from flask import Blueprint, Flask
from jinja2 import Environment

import progressive_scan


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _frame(**overrides):
    item = {
        "recommendations": "Biofocal or Progressive Selection), Multi Anti-Glare",
        "product_category": "Spectacles Frame",
    }
    item.update(overrides)
    return item


class FakeCursor:
    def __init__(self, scan_row=None, link_candidates=None):
        self.scan_row = scan_row
        self.choice_row = None
        self.link_candidates = list(link_candidates or [])
        self.calls = []
        self.history = []
        self.updates = []
        self.fetchone_result = None
        self.fetchall_result = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("select id from face_measurements"):
            self.fetchone_result = self.scan_row
        elif normalized.startswith("select choice,"):
            self.fetchone_result = self.choice_row
        elif "insert into order_face_scan_choice" in normalized:
            previous = self.choice_row or {}
            if params[2] == progressive_scan.SCANNED:
                self.choice_row = {
                    "choice": params[2],
                    "warning_version": params[3] or previous.get("warning_version"),
                    "warning_text": params[4] or previous.get("warning_text"),
                    "warned_at": params[5] or previous.get("warned_at"),
                    "decided_at": previous.get("decided_at") or params[6],
                    "face_measurement_id": params[7],
                    "previous_choice": params[8],
                }
            else:
                self.choice_row = {
                    "choice": params[2],
                    "warning_version": params[3],
                    "warning_text": params[4],
                    "warned_at": previous.get("warned_at") or params[5],
                    "decided_at": previous.get("decided_at") or params[6],
                    "previous_choice": params[7],
                }
        elif "insert into order_history" in normalized:
            self.history.append(params)
        elif "select distinct c.order_id" in normalized:
            shipped = set(params[2:])
            self.fetchall_result = [
                row for row in self.link_candidates
                if row.get("status") not in shipped
            ]
        elif normalized.startswith("update order_face_scan_choice"):
            self.updates.append((sql, params))
            row = next((candidate for candidate in self.link_candidates
                        if candidate["order_id"] == params[3]), None)
            if row:
                row.update({
                    "choice": params[0],
                    "previous_choice": params[1],
                    "face_measurement_id": params[2],
                    "scanned_at": "NOW",
                })
        return None

    def fetchone(self):
        result = self.fetchone_result
        self.fetchone_result = None
        return result

    def fetchall(self):
        result = self.fetchall_result
        self.fetchall_result = []
        return result


class ProgressiveScanTests(unittest.TestCase):
    def test_applies_to_progressive_bifocal_and_bare_choice_with_additions(self):
        self.assertTrue(progressive_scan.applies(_frame()))
        self.assertTrue(progressive_scan.applies(
            _frame(recommendations="Biofocal or Progressive Selection), Flat Top Bifocal")))
        self.assertTrue(progressive_scan.applies(
            _frame(recommendations="Biofocal or Progressive Selection)",
                   addon_1_name="Multi Anti-Glare")))
        self.assertTrue(progressive_scan.cart_needs_step([
            {"recommendations": "Single Vision", "product_category": "Spectacles Frame"},
            _frame(),
        ]))

    def test_applies_rejects_non_progressive_and_non_frame_lines(self):
        self.assertFalse(progressive_scan.applies(None))
        self.assertFalse(progressive_scan.applies(
            _frame(recommendations="Single Vision Selection)")))
        self.assertFalse(progressive_scan.applies(
            _frame(product_category="Spectacles Lens")))
        self.assertFalse(progressive_scan.applies(
            _frame(product_category="Contact Lenses")))
        self.assertFalse(progressive_scan.applies(
            _frame(product_category="Spectacles Frame", vertical="CONTACT_LENS")))
        self.assertFalse(progressive_scan.applies(
            _frame(product_category="AI Glasses")))
        self.assertFalse(progressive_scan.cart_needs_step([]))

    def test_schema_and_latest_scan_lookup(self):
        schema_was_ready = progressive_scan._SCHEMA_READY
        progressive_scan._SCHEMA_READY = False
        self.addCleanup(setattr, progressive_scan, "_SCHEMA_READY",
                        schema_was_ready)
        cur = FakeCursor(scan_row={"id": 73})
        progressive_scan.ensure_schema(cur)
        self.assertIn("order_face_scan_choice", cur.calls[0][0])
        self.assertNotIn("FOREIGN KEY", cur.calls[0][0].upper())
        self.assertEqual(progressive_scan.latest_scan_id(cur, 17), 73)
        self.assertIsNone(progressive_scan.latest_scan_id(cur, None))

    def test_checkout_acknowledgement_requires_warning_unless_scan_exists(self):
        self.assertFalse(progressive_scan.acknowledgement_is_valid(None, None, None))
        self.assertFalse(progressive_scan.acknowledgement_is_valid(None, "1", None))
        self.assertTrue(progressive_scan.acknowledgement_is_valid(None, "1", "warned"))
        self.assertTrue(progressive_scan.acknowledgement_is_valid(73, None, None))
        with open(os.path.join(REPO, "models.py"), encoding="utf-8") as source_file:
            source = source_file.read()
        helper = source.split("def _progressive_scan_checkout_allowed(cart):", 1)[1]
        helper = helper.split("\n\n@bp.route('/checkout'", 1)[0]
        self.assertIn("acknowledgement_is_valid", helper)
        self.assertIn(
            "Please take the face scan, or tick “I understand” to continue without it.",
            helper)

    def test_checkout_get_only_builds_the_step_when_scan_is_missing(self):
        with open(os.path.join(REPO, "models.py"), encoding="utf-8") as source_file:
            source = source_file.read()
        route = source.split("def checkout_page():", 1)[1].split(
            "\n@bp.route", 1)[0]
        self.assertIn("face_scan_step = None", route)
        self.assertIn("if progressive_scan.cart_needs_step(cart):", route)
        self.assertIn("latest_scan_id(_pf_cursor, user_id)", route)

    def test_record_scanned_choice_without_history(self):
        cur = FakeCursor()
        result = progressive_scan.record(
            cur, "ORDER-1", 17, "optiwar.com", [_frame()], 73, None, None)
        self.assertEqual(result, progressive_scan.SCANNED)
        self.assertEqual(cur.choice_row["face_measurement_id"], 73)
        self.assertEqual(cur.history, [])
        self.assertIn("ON DUPLICATE KEY UPDATE", cur.calls[-1][0])

    def test_single_vision_checkout_does_not_create_a_choice(self):
        cur = FakeCursor()
        result = progressive_scan.record(
            cur, "ORDER-3", 17, "optiwar.com",
            [_frame(recommendations="Single Vision Selection)")],
            None, None, None)
        self.assertIsNone(result)
        self.assertEqual(cur.calls, [])

    def test_record_decline_with_warning_and_idempotent_history(self):
        cur = FakeCursor()
        warned = "2026-01-02T03:04:05"
        accepted = "2026-01-02T03:05:06"
        first = progressive_scan.record(
            cur, "ORDER-2", 17, "in.optiwar.com", [_frame()], None,
            warned, accepted)
        second = progressive_scan.record(
            cur, "ORDER-2", 17, "in.optiwar.com", [_frame()], None,
            warned, accepted)
        self.assertEqual(first, progressive_scan.DECLINED_AFTER_WARNING)
        self.assertEqual(second, progressive_scan.DECLINED_AFTER_WARNING)
        self.assertEqual(cur.choice_row["warning_version"],
                         progressive_scan.WARNING_VERSION)
        self.assertEqual(cur.choice_row["warning_text"], progressive_scan.WARNING)
        self.assertEqual(cur.choice_row["warned_at"].isoformat(), warned)
        self.assertEqual(cur.choice_row["decided_at"].isoformat(), accepted)
        self.assertEqual([row[0] for row in cur.history],
                         [progressive_scan.HISTORY_DECLINED])

    def test_later_scan_updates_only_unshipped_declines_and_keeps_history_data(self):
        open_order = {
            "order_id": "OPEN", "site_from": "optiwar.com",
            "status": "Processed",
            "choice": progressive_scan.DECLINED_AFTER_WARNING,
            "warned_at": "warned", "decided_at": "accepted",
            "warning_text": progressive_scan.WARNING,
        }
        shipped_order = {
            "order_id": "SHIPPED", "site_from": "optiwar.com",
            "status": "Shipped",
            "choice": progressive_scan.DECLINED_AFTER_WARNING,
            "warned_at": "warned", "decided_at": "accepted",
            "warning_text": progressive_scan.WARNING,
        }
        cur = FakeCursor(link_candidates=[open_order, shipped_order])
        progressive_scan.link_later_scan(cur, 17, 83)
        self.assertEqual(len(cur.updates), 1)
        sql, params = cur.updates[0]
        self.assertEqual(params[3], "OPEN")
        self.assertIn("previous_choice", sql)
        self.assertNotIn("warned_at=", sql)
        self.assertNotIn("decided_at=", sql)
        self.assertEqual(cur.history[0][0], progressive_scan.HISTORY_LATER_SCAN)
        self.assertEqual(cur.history[0][1:], ("OPEN", "optiwar.com"))
        self.assertEqual(open_order["choice"], progressive_scan.SCANNED)
        self.assertEqual(open_order["previous_choice"],
                         progressive_scan.DECLINED_AFTER_WARNING)
        self.assertEqual(open_order["face_measurement_id"], 83)
        self.assertEqual(open_order["warned_at"], "warned")
        self.assertEqual(open_order["decided_at"], "accepted")
        self.assertEqual(open_order["warning_text"], progressive_scan.WARNING)
        self.assertEqual(shipped_order["choice"],
                         progressive_scan.DECLINED_AFTER_WARNING)

    def test_later_scan_link_failure_is_logged_and_swallowed(self):
        class BrokenCursor:
            def execute(self, *_args, **_kwargs):
                raise RuntimeError("missing table")

        with self.assertLogs("progressive_scan", level="WARNING"):
            progressive_scan.link_later_scan(BrokenCursor(), 17, 83)

    def test_event_endpoint_orders_warned_and_accepted_and_requires_login(self):
        app = Flask(__name__)
        app.secret_key = "test"
        bp = Blueprint("main", __name__)
        progressive_scan.register(bp)
        app.register_blueprint(bp)
        client = app.test_client()

        response = client.post("/checkout/face-scan/event",
                               data={"event": "warned"})
        self.assertEqual(response.status_code, 401)

        with client.session_transaction() as sess:
            sess["user_id"] = 17
        response = client.post("/checkout/face-scan/event",
                               data={"event": "accepted"})
        self.assertEqual(response.status_code, 200)
        with client.session_transaction() as sess:
            self.assertNotIn("pf_accepted_at", sess)

        client.post("/checkout/face-scan/event", data={"event": "warned"})
        with client.session_transaction() as sess:
            warned_at = sess["pf_warned_at"]
        client.post("/checkout/face-scan/event", data={"event": "warned"})
        client.post("/checkout/face-scan/event", data={"event": "accepted"})
        with client.session_transaction() as sess:
            self.assertEqual(sess["pf_warned_at"], warned_at)
            self.assertTrue(sess.get("pf_accepted_at"))

    def test_tryon_return_accepts_only_safe_relative_paths(self):
        self.assertEqual(progressive_scan.safe_return_path("/checkout"), "/checkout")
        self.assertIsNone(progressive_scan.safe_return_path("//evil.com"))
        self.assertIsNone(progressive_scan.safe_return_path("https://x"))
        self.assertIsNone(progressive_scan.safe_return_path("/\\evil.com"))
        with open(os.path.join(REPO, "models.py"), encoding="utf-8") as source_file:
            source = source_file.read()
        route = source.split("def spectacle_tryon():", 1)[1].split(
            "\n@bp.route", 1)[0]
        self.assertIn("request.full_path.rstrip('?')", route)
        self.assertIn("safe_return_path", route)
        with open(os.path.join(REPO, "templates", "tryon.html"),
                  encoding="utf-8") as template_file:
            tryon_template = template_file.read()
        with open(os.path.join(REPO, "static", "tryon", "js", "tryon.js"),
                  encoding="utf-8") as script_file:
            tryon_script = script_file.read()
        self.assertIn("Back to checkout", tryon_template)
        self.assertIn("backToCheckout.hidden = false", tryon_script)

    def test_checkout_template_renders_the_step_only_when_requested(self):
        with open(os.path.join(REPO, "templates", "checkout.html"),
                  encoding="utf-8") as template_file:
            source = template_file.read()
        policy_start = source.index(
            '<div class="ow-ck-policy" id="owPolicyBlock">')
        scan_start = source.index(
            '<section class="ow-pf-scan" id="owProgressiveScan"')
        self.assertGreater(scan_start, policy_start)
        scan_markup = source[scan_start:source.index("</section>", scan_start)]
        self.assertIn('class="ow-pf-scan-ack"', scan_markup)
        self.assertNotIn("ow-ck-policy-accept", scan_markup)
        with open(os.path.join(REPO, "static", "css", "ow-ui.css"),
                  encoding="utf-8") as css_file:
            css = css_file.read()
        self.assertIn(
            ".ow-ck.ow-mob-step-3 .ow-ck-policy-accept { display: none !important; }",
            css)
        self.assertNotIn(
            ".ow-ck.ow-mob-step-3 .ow-pf-scan-ack", css)
        match = re.search(
            r"({% if face_scan_step %}.*?{% endif %})", source, re.S)
        self.assertIsNotNone(match)
        template = Environment(autoescape=True).from_string(match.group(1))
        step = {
            "heading": progressive_scan.HEADING,
            "body": progressive_scan.BODY,
            "btn_scan": progressive_scan.BTN_SCAN,
            "btn_skip": progressive_scan.BTN_SKIP,
            "warning": progressive_scan.WARNING,
            "tick": progressive_scan.TICK,
            "tryon_url": "/tryon?return=%2Fcheckout",
        }
        rendered = template.render(
            face_scan_step=step,
            url_for=lambda endpoint: "/" + endpoint.rsplit(".", 1)[-1])
        self.assertIn(progressive_scan.HEADING, rendered)
        self.assertIn(progressive_scan.WARNING, rendered)
        self.assertIn('name="face_scan_ack" value="1"', rendered)
        self.assertNotIn(progressive_scan.HEADING,
                         template.render(face_scan_step=None, url_for=lambda *_a: "/"))

    def test_ops_template_renders_decline_and_later_scan_choices(self):
        with open(os.path.join(REPO, "templates", "ops_order_face_scan.html"),
                  encoding="utf-8") as template_file:
            template = Environment(autoescape=True).from_string(
                template_file.read())
        result = {
            "order_id": "ORDER-1",
            "customer_id": 17,
            "date_created": "2026-01-02",
            "site_from": "optiwar.com",
            "status": "NO_SCAN",
            "warnings": [],
            "lines": [],
            "people": [],
        }
        result["customer_choice"] = {
            "choice": "DECLINED_AFTER_WARNING",
            "warned_at": "2026-01-02T03:04:05",
            "decided_at": "2026-01-02T03:06:07",
            "warning_text": progressive_scan.WARNING,
        }
        declined = template.render(result=result, order="ORDER-1", not_found=False)
        self.assertIn("Customer chose no face scan, warned 2026-01-02T03:04:05, "
                      "accepted 2026-01-02T03:06:07", declined)
        self.assertIn(progressive_scan.WARNING, declined)

        result["customer_choice"] = {
            "choice": "SCANNED",
            "previous_choice": "DECLINED_AFTER_WARNING",
            "warned_at": "2026-01-02T03:04:05",
        }
        later = template.render(result=result, order="ORDER-1", not_found=False)
        self.assertIn("Face scan taken after ordering (earlier declined, warned "
                      "2026-01-02T03:04:05)", later)

        result["customer_choice"] = {"choice": "SCANNED"}
        scanned = template.render(result=result, order="ORDER-1", not_found=False)
        self.assertIn("Face scan on file at checkout", scanned)

    def test_legacy_tryon_save_links_the_inserted_id_without_schema_setup(self):
        with open(os.path.join(REPO, "models.py"), encoding="utf-8") as source_file:
            source = source_file.read()
        route = source.split("def api_tryon_save():", 1)[1].split(
            "\n@bp.route", 1)[0]
        self.assertNotIn("progressive_scan.ensure_schema", route)
        self.assertLess(route.index("face_measurement_id = cursor.lastrowid"),
                        route.index("progressive_scan.link_later_scan("))
        self.assertIn(
            "cursor, customer_id, face_measurement_id)", route)


if __name__ == "__main__":
    unittest.main()
