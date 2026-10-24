"""Regression coverage for chat Rx-photo retention and action vocabulary."""
import base64
import datetime
import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

try:
    ta = importlib.import_module("test_chat_attachments")
except ImportError:
    ta = importlib.import_module("tests.test_chat_attachments")

if "flask_mail" not in sys.modules:
    flask_mail = types.ModuleType("flask_mail")
    flask_mail.Message = object
    sys.modules["flask_mail"] = flask_mail

import acr


class _Cursor:
    def __init__(self, db):
        self.db = db
        self.rows = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        if sql.startswith("SELECT id,stored_name"):
            cutoff = params[0]
            self.rows = [row.copy() for row in self.db.rows.values()
                         if row["created_at"] < cutoff and row["stored_name"]
                         and row["purged_at"] is None]
        elif sql.startswith("UPDATE chat_attachments SET stored_name"):
            purged_at, row_id = params
            self.db.rows[row_id]["stored_name"] = ""
            self.db.rows[row_id]["purged_at"] = purged_at
            self.rowcount = 1
        elif sql.startswith("SELECT session_id,mime_type,stored_name,purged_at"):
            row = self.db.rows.get(params[0])
            self.rows = [row.copy()] if row else []
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _DB:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass

    def close(self):
        pass


class _ConfirmationDB:
    def __init__(self, attachment):
        self.attachment = attachment

    def cursor(self):
        return self

    def execute(self, _sql, _params=()):
        pass

    def fetchone(self):
        return self.attachment

    def commit(self):
        pass


@unittest.skipUnless(ta.AVAILABLE, "no MariaDB test database (see scripts/setup_test_db.sh)")
class ChatRxPhotoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not hasattr(ta.OnMariaDB, "cg"):
            ta.OnMariaDB.setUpClass()
        cls.cg = ta.OnMariaDB.cg
        cls.app = ta.OnMariaDB.app
        cls.cv = ta.OnMariaDB.cg.chat_vision
        cls.ordx = importlib.import_module("flaskr.order_rx_documents")
        cls.cg.acr.ensure_schema(ta._connect)
        db = ta._connect()
        try:
            cur = db.cursor()
            for col, ddl in (("customer_id", "INT NULL"),
                             ("current_page_url", "TEXT NULL"),
                             ("ket_ticket_uid", "VARCHAR(191) NULL"),
                             ("ket_ticket_ref", "VARCHAR(191) NULL")):
                cur.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS %s %s"
                            % (col, ddl))
            cur.execute(
                """CREATE TABLE IF NOT EXISTS chat_rx_photo_test_tickets (
                       id BIGINT AUTO_INCREMENT PRIMARY KEY,
                       session_id VARCHAR(64) NOT NULL,
                       name VARCHAR(191) NULL,
                       email VARCHAR(191) NULL,
                       subject VARCHAR(255) NULL,
                       message TEXT NULL,
                       KEY idx_session (session_id)
                   ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        finally:
            db.close()

    def _ticket_turn(self, answer, tampered=False):
        fixture = ta.OnMariaDB("runTest")
        fixture.db = ta._connect()
        fixture.setUp()
        self.addCleanup(fixture.db.close)
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

        def clean_ticket_rows():
            cur = fixture.db.cursor()
            for table in ("ai_actions", "ai_events", "chat_rx_photo_test_tickets"):
                cur.execute("DELETE FROM %s WHERE session_id=%%s" % table,
                            (fixture.sid,))
        self.addCleanup(clean_ticket_rows)

        def create_ticket(name, email, subject, message):
            cur = fixture.db.cursor()
            cur.execute(
                "INSERT INTO chat_rx_photo_test_tickets "
                "(session_id,name,email,subject,message) VALUES (%s,%s,%s,%s,%s)",
                (fixture.sid, name, email, subject, message))
            return cur.lastrowid

        ticket_patch = mock.patch.object(
            self.cg, "create_ticket_in_db", side_effect=create_ticket)
        ticket_patch.start()
        self.addCleanup(ticket_patch.stop)
        for name in ("_send_ticket_whatsapp", "_send_ticket_sms"):
            notification_patch = mock.patch.object(self.cg, name)
            notification_patch.start()
            self.addCleanup(notification_patch.stop)
        forwarded = fixture._ticket_flow()
        fixture._as_owner()
        fixture.db.cursor().execute(
            "UPDATE chat_sessions SET customer_id=101 WHERE session_id=%s",
            (fixture.sid,))
        fixture.reading = self.cv.parse(
            '{"kind":"prescription","confidence":0.96,"prescription":'
            '{"right":{"sph":"-1.00"},"left":{"sph":"-1.00"}}}')
        line = {"order_id": "OW-22", "order_line_id": 3, "product_id": 4,
                "rx_id": 5, "product_name": "Frame", "typed": {}}
        preparing = mock.patch.object(
            self.ordx, "preparing_rx_lines",
            side_effect=[[line], [] if tampered else [line]])
        lens = mock.patch.object(
            self.cg, "_lens_context", return_value=(None, None, None, None))
        test_identity = mock.patch.object(
            self.cg.test_identity, "is_test_customer", return_value=False)
        model_patch = mock.patch.object(
            self.cg, "_call_deepseek",
            return_value=("unexpected model reply", None))
        with preparing, lens, test_identity, model_patch as model:
            offered = fixture._post(ta.PNG, name="rx.png").get_json()
            self.assertIn("Shall I add this photo to order OW-22", offered["reply"])
            self.assertEqual(offered["actions"], [])
            cur = fixture.db.cursor()
            cur.execute(
                "SELECT action_id,action_type,target,status FROM ai_actions "
                "WHERE session_id=%s", (fixture.sid,))
            action = cur.fetchone()
            self.assertEqual(action["action_type"], "RX_PHOTO_ATTACH")
            self.assertEqual(action["status"], "PENDING")
            response = fixture.client.post("/api/chat/message", json={
                "session_id": fixture.sid, "content": answer,
                "page_url": "https://optiwar.com/account/orders",
                "client_message_id": "rx-" + answer + ("-tampered" if tampered else ""),
            })
            self.assertEqual(response.status_code, 200)
            model.assert_not_called()
        return fixture, forwarded, response.get_json()

    def _assert_ticket_and_photo_were_forwarded(self, fixture, forwarded):
        cur = fixture.db.cursor()
        cur.execute(
            "SELECT id,name,email,subject,message FROM chat_rx_photo_test_tickets "
            "WHERE session_id=%s", (fixture.sid,))
        ticket = cur.fetchone()
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket["email"], "jane@example.com")
        self.assertIn("[Photo attached: rx.jpg]", ticket["message"])
        self.assertEqual(len(forwarded), 1)
        images = forwarded[0]["images"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["filename"], "rx.jpg")
        self.assertEqual(
            base64.b64decode(images[0]["data_base64"])[:3], b"\xff\xd8\xff")

    def test_decline_message_turn_creates_ticket_and_forwards_photo(self):
        fixture, forwarded, body = self._ticket_turn("no")
        self.assertEqual(body["actions"], ["create_ticket"])
        self.assertEqual(body["rx_photo_action"]["state"], "DECLINED")
        self.assertIn("Your support ticket KET-31 has been created", body["reply"])
        self._assert_ticket_and_photo_were_forwarded(fixture, forwarded)

    def test_nonpreparing_target_failure_creates_ticket_and_forwards_photo(self):
        fixture, forwarded, body = self._ticket_turn("yes", tampered=True)
        self.assertEqual(body["actions"], ["create_ticket"])
        self.assertEqual(body["rx_photo_action"]["state"], "FAILED")
        self.assertEqual(body["rx_photo_action"]["code"], "not_preparing")
        self.assertIn("Your support ticket KET-31 has been created", body["reply"])
        self._assert_ticket_and_photo_were_forwarded(fixture, forwarded)

    def test_action_offer_vocabulary_and_prompt_rule(self):
        self.assertIn("RX_PHOTO_ATTACH", acr.SUPPORT_ACTION_TYPES)
        self.assertEqual(acr.EV_RX_PHOTO_ATTACH_OFFERED, "RX_PHOTO_ATTACH_OFFERED")
        with open(os.path.join(REPO, "chat_gateway.py"), encoding="utf-8") as prompt:
            source = prompt.read()
        self.assertIn(
            "after paying (or any time), attach it here with the photo button "
            "(paperclip) next to the message box", source)
        self.assertIn('Never mention an "Upload Prescription" button', source)

    def test_vision_confirmation_guard_is_preserved(self):
        self.assertTrue(any("Nothing is applied until you confirm." in value
                            for value in self.cv.customer_reply.__code__.co_consts
                            if isinstance(value, str)))

    def test_confirmation_attaches_existing_reading_without_a_new_vision_call(self):
        attachment = {
            "id": 19, "session_id": "rx-session", "filename": "rx.jpg",
            "mime_type": "image/jpeg", "stored_name": "chatimg-19.jpg",
            "purged_at": None, "vision_model": "vision-model",
            "vision_json": (
                '{"kind":"prescription","confidence":0.96,"proposal":'
                '{"right":{"sph":"-1.00"},"left":{"sph":"-1.00"}}}'),
        }
        pending = {"action_id": 77, "target": "19:OW-22:3"}
        line = {"order_id": "OW-22", "order_line_id": 3, "product_id": 4,
                "rx_id": 5, "product_name": "Frame", "typed": {}}
        db = _ConfirmationDB(attachment)
        attached = []
        outcomes = []
        with (mock.patch.object(self.cg.acr, "get_live_pending_action_of",
                                return_value=pending),
              mock.patch.object(self.cg.acr, "mark_action", return_value=True),
              mock.patch.object(self.cg.face_assistant, "record_outcome",
                                side_effect=lambda *a, **k: outcomes.append(a)),
              mock.patch.object(self.ordx, "preparing_rx_lines",
                                return_value=[line]),
              mock.patch.object(self.ordx, "ensure_schema"),
              mock.patch.object(self.ordx, "attach_chat_photo",
                                side_effect=lambda *a: attached.append(a) or 91),
              mock.patch.object(self.cg, "_read_attachment_bytes",
                                return_value=b"existing photo"),
              mock.patch.object(self.cg.chat_vision, "describe") as describe):
            reply, result, actions = self.cg._rx_photo_confirmation(
                db, "rx-session", {"customer_id": 101}, "yes", "/account/orders")
        self.assertEqual(result["state"], "EXECUTED")
        self.assertEqual(result["order_id"], "OW-22")
        self.assertEqual(actions, [])
        self.assertIn("order OW-22", reply)
        self.assertNotIn("-1.00", reply)
        self.assertEqual(len(attached), 1)
        self.assertEqual(attached[0][2:4], (19, line))
        describe.assert_not_called()
        self.assertTrue(outcomes)

    def test_a_confident_reading_offers_the_newest_order_without_writing(self):
        reading = self.cv.parse(
            '{"kind":"prescription","confidence":0.9,"prescription":'
            '{"right":{"sph":"-1.00"},"left":{"sph":"-1.00"}}}')
        line = {"order_id": "OW-22", "order_line_id": 3, "product_id": 4,
                "rx_id": 5, "product_name": "Frame"}
        actions = []
        with (mock.patch.object(self.cg.chat_vision, "describe",
                                return_value=(reading, "vision-model")),
              mock.patch.object(self.cg, "_lens_context",
                                return_value=(None, None, None, None)),
              mock.patch.object(self.ordx, "preparing_rx_lines",
                                return_value=[line]),
              mock.patch.object(self.cg.acr, "create_pending_action",
                                return_value=77) as create,
              mock.patch.object(self.cg.acr, "log_event") as log_event):
            reply, offered, lens_proposal = self.cg._answer_photo(
                _ConfirmationDB(None), {"customer_id": 101}, "rx-session",
                19, {"filename": "rx.jpg", "mime_type": "image/jpeg"},
                b"existing photo", "/account/orders", {})
        self.assertEqual(offered, [])
        self.assertIsNone(lens_proposal)
        self.assertIn(
            "Shall I add this photo to order OW-22 (Frame) so our team checks",
            reply)
        create.assert_called_once()
        self.assertEqual(create.call_args.args[2:4],
                         ("RX_PHOTO_ATTACH", "19:OW-22:3"))
        self.assertEqual(create.call_args.kwargs["offer_event"],
                         self.cg.acr.EV_RX_PHOTO_ATTACH_OFFERED)
        self.assertEqual(create.call_args.kwargs["supersede_types"],
                         ("RX_PHOTO_ATTACH",))
        log_event.assert_not_called()

    def test_signed_out_or_ticketed_sessions_do_not_receive_an_order_offer(self):
        reading = self.cv.parse(
            '{"kind":"prescription","confidence":0.9,"prescription":'
            '{"right":{"sph":"-1.00"},"left":{"sph":"-1.00"}}}')
        with (mock.patch.object(self.cg.chat_vision, "describe",
                                return_value=(reading, "vision-model")),
              mock.patch.object(self.cg, "_lens_context",
                                return_value=(None, None, None, None)),
              mock.patch.object(self.ordx, "preparing_rx_lines", return_value=[]),
              mock.patch.object(self.cg.acr, "create_pending_action") as create,
              mock.patch.object(self.cg.acr, "log_event") as log_event):
            reply, actions, _ = self.cg._answer_photo(
                _ConfirmationDB(None), {}, "guest-session", 21,
                {"filename": "rx.jpg", "mime_type": "image/jpeg"},
                b"photo", "/checkout", {})
        self.assertEqual(actions, ["create_ticket"])
        self.assertIn("passed it to our support team", reply)
        self.assertEqual(log_event.call_args.kwargs["action_type"], "PHOTO_ESCALATION")
        self.assertEqual(log_event.call_args.kwargs["payload"]["reason"],
                         "rx_photo_no_order")
        create.assert_not_called()

        with (mock.patch.object(self.cg.chat_vision, "describe",
                                return_value=(reading, "vision-model")),
              mock.patch.object(self.cg, "_lens_context",
                                return_value=(None, None, None, None)),
              mock.patch.object(self.ordx, "preparing_rx_lines", return_value=[]),
              mock.patch.object(self.cg.acr, "log_event") as log_event):
            reply, actions, _ = self.cg._answer_photo(
                _ConfirmationDB(None), {"ket_ticket_uid": "ticket-9"},
                "ticketed-session", 22,
                {"filename": "rx.jpg", "mime_type": "image/jpeg"},
                b"photo", "/checkout", {})
        self.assertEqual(actions, [])
        self.assertIn("on your open ticket", reply)
        log_event.assert_not_called()

    def test_decline_and_foreign_attachment_fall_back_to_support(self):
        pending = {"action_id": 78, "target": "20:OW-23:4"}
        with (mock.patch.object(self.cg.acr, "get_live_pending_action_of",
                                return_value=pending),
              mock.patch.object(self.cg.acr, "mark_action", return_value=True) as mark):
            reply, result, actions = self.cg._rx_photo_confirmation(
                _ConfirmationDB(None), "rx-session",
                {"customer_id": 101}, "no", "/")
        self.assertEqual(result["state"], "DECLINED")
        self.assertEqual(actions, ["create_ticket"])
        self.assertIn("haven't added it to an order", reply)
        mark.assert_called_once()

        attachment = {"id": 20, "session_id": "somebody-else",
                      "stored_name": "chatimg-20.jpg", "purged_at": None}
        outcomes = []
        with (mock.patch.object(self.cg.acr, "get_live_pending_action_of",
                                return_value=pending),
              mock.patch.object(self.cg.acr, "mark_action", return_value=True),
              mock.patch.object(self.cg.face_assistant, "record_outcome",
                                side_effect=lambda *a, **k: outcomes.append(a))):
            reply, result, actions = self.cg._rx_photo_confirmation(
                _ConfirmationDB(attachment), "rx-session",
                {"customer_id": 101}, "yes", "/")
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["code"], "not_your_attachment")
        self.assertEqual(actions, ["create_ticket"])
        self.assertIn("passed it to our support team", reply)
        self.assertTrue(outcomes)

    def test_purge_keeps_recent_photos_and_get_returns_404_for_expired(self):
        now = datetime.datetime(2025, 2, 1, 12, 0, 0)
        with tempfile.TemporaryDirectory(prefix="chat-rx-purge-") as directory:
            old_path = os.path.join(directory, "chatimg-1.jpg")
            recent_path = os.path.join(directory, "chatimg-2.jpg")
            with open(old_path, "wb") as image:
                image.write(b"old")
            with open(recent_path, "wb") as image:
                image.write(b"recent")
            rows = {
                1: {"id": 1, "session_id": "old-session", "mime_type": "image/jpeg",
                    "stored_name": "chatimg-1.jpg",
                    "created_at": now - datetime.timedelta(days=31),
                    "purged_at": None},
                2: {"id": 2, "session_id": "recent-session", "mime_type": "image/jpeg",
                    "stored_name": "chatimg-2.jpg",
                    "created_at": now - datetime.timedelta(days=5),
                    "purged_at": None},
            }
            db = _DB(rows)
            old_dir = self.cg._attachments_dir
            old_get_db = self.cg._get_db
            self.cg._attachments_dir = lambda: directory
            self.cg._get_db = lambda: db
            try:
                with self.app.app_context():
                    self.assertEqual(self.cg.purge_expired(db, now=now), 1)
                self.assertFalse(os.path.exists(old_path))
                self.assertTrue(os.path.exists(recent_path))
                self.assertEqual(rows[1]["stored_name"], "")
                self.assertIsNotNone(rows[1]["purged_at"])
                self.assertEqual(
                    self.app.test_client().get("/api/chat/attachment/1").status_code,
                    404)
            finally:
                self.cg._attachments_dir = old_dir
                self.cg._get_db = old_get_db


if __name__ == "__main__":
    unittest.main()
