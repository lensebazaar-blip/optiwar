"""Regression coverage for chat Rx-photo retention and action vocabulary."""
import base64
import datetime
import importlib
import json
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
    def __init__(self, attachment, assistant=None):
        self.attachment = attachment
        self.assistant = assistant
        self.last_sql = ""

    def cursor(self):
        return self

    def execute(self, sql, _params=()):
        self.last_sql = sql

    def fetchone(self):
        if "FROM chat_messages" in self.last_sql:
            return self.assistant
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
        cls.ordx._SCHEMA_READY = False
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
                """CREATE TABLE IF NOT EXISTS rx_collector (
                       rx_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                       recommendations VARCHAR(255) NULL,
                       recommendation_price INT NULL,
                       right_eye VARCHAR(255) NULL,
                       left_eye VARCHAR(255) NULL,
                       product_id INT NULL,
                       date_created DATETIME DEFAULT CURRENT_TIMESTAMP
                   ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            for col, ddl in (("recommendations", "VARCHAR(255) NULL"),
                             ("recommendation_price", "INT NULL"),
                             ("right_eye", "VARCHAR(255) NULL"),
                             ("left_eye", "VARCHAR(255) NULL"),
                             ("product_id", "INT NULL"),
                             ("date_created", "DATETIME NULL DEFAULT CURRENT_TIMESTAMP")):
                cur.execute("ALTER TABLE rx_collector ADD COLUMN IF NOT EXISTS %s %s"
                            % (col, ddl))
            cur.execute(
                "ALTER TABLE rx_collector MODIFY rx_id INT NOT NULL AUTO_INCREMENT")
            cls.ordx.ensure_schema(cur)
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

    def _typed_from_rx_row(self, row):
        values = {}
        for eye, column in (("right", "right_eye"), ("left", "left_eye")):
            parsed = self.ordx.rx_lookup.parse_eye(row.get(column))
            values[eye + "_pwr"] = parsed.get("sph") if parsed else None
            values[eye + "_cyl"] = parsed.get("cyl") if parsed else None
            values[eye + "_axis"] = parsed.get("axis") if parsed else None
            values[eye + "_add"] = parsed.get("add") if parsed else None
        return self.ordx.typed_snapshot(values)

    def _ticket_turn(self, answer, tampered=False, *, vision_proposal=None,
                     ticketed=False, switch_user_to=None, intervening=False,
                     attach_failure=False, ket_upload_answers=None):
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
        with fixture.client.session_transaction() as browser_session:
            browser_session["user_id"] = 101
        cur = fixture.db.cursor()
        cur.execute(
            "UPDATE chat_sessions SET customer_id=101,ket_ticket_uid=%s,"
            "ket_ticket_ref=%s WHERE session_id=%s",
            ("uid-31" if ticketed else None,
             "KET-31" if ticketed else None, fixture.sid))
        if ket_upload_answers:
            fixture.answers.extend(ket_upload_answers)
        right_eye = "+1.25/-0.50/180/1.50"
        left_eye = "-2.00/-0.25/90/1.50"
        cur.execute(
            "INSERT INTO rx_collector (right_eye,left_eye,product_id) "
            "VALUES (%s,%s,%s)", (right_eye, left_eye, 4))
        rx_id = cur.lastrowid
        self.addCleanup(lambda: fixture.db.cursor().execute(
            "DELETE FROM rx_collector WHERE rx_id=%s", (rx_id,)))
        cur.execute("SELECT right_eye,left_eye FROM rx_collector WHERE rx_id=%s",
                    (rx_id,))
        rx_row = cur.fetchone()
        typed = self._typed_from_rx_row(rx_row)
        line = {"order_id": "OW-22", "order_line_id": 3, "product_id": 4,
                "rx_id": rx_id, "product_name": "Frame", "typed": typed}
        fixture.rx_line = line
        fixture.rx_typed = typed
        fixture.rx_id = rx_id
        vision_proposal = vision_proposal or {
            "right": {"sph": "+1.25", "cyl": "-0.50", "axis": "180",
                      "add": "1.50"},
            "left": {"sph": "-2.00", "cyl": "-0.25", "axis": "90",
                     "add": "1.50"},
        }
        fixture.reading = self.cv.parse(json.dumps({
            "kind": "prescription", "confidence": 0.96,
            "prescription": vision_proposal}))
        preparing = mock.patch.object(
            self.ordx, "preparing_rx_lines",
            side_effect=[[line], [] if tampered else [line]])
        lens = mock.patch.object(
            self.cg, "_lens_context", return_value=(None, None, None, None))
        test_identity = mock.patch.object(
            self.cg.test_identity, "is_test_customer", return_value=False)
        model_patch = mock.patch.object(
            self.cg, "_call_deepseek",
            return_value=("Is there anything else I can help with?", None))
        failed_document_ids = []
        write_failure = None
        if attach_failure:
            def fail_write(document_id, _pages):
                failed_document_ids.append(document_id)
                raise OSError("storage unavailable")
            write_failure = mock.patch.object(
                self.ordx, "_write_pages", side_effect=fail_write)
            write_failure.start()
            self.addCleanup(write_failure.stop)
        with preparing, lens, test_identity, model_patch as model:
            offered = fixture._post(ta.PNG, name="rx.png").get_json()
            fixture.rx_offer = offered
            self.assertIn("Shall I add this photo to order OW-22", offered["reply"])
            self.assertEqual(offered["actions"], [])
            cur = fixture.db.cursor()
            cur.execute(
                "SELECT action_id,action_type,target,status FROM ai_actions "
                "WHERE session_id=%s", (fixture.sid,))
            action = cur.fetchone()
            self.assertEqual(action["action_type"], "RX_PHOTO_ATTACH")
            self.assertEqual(action["status"], "PENDING")
            if intervening:
                intermediate = fixture.client.post("/api/chat/message", json={
                    "session_id": fixture.sid,
                    "content": "Can you tell me a joke?",
                    "page_url": "https://optiwar.com/account/orders",
                    "client_message_id": "rx-unrelated",
                })
                self.assertEqual(intermediate.status_code, 200)
            if switch_user_to is not None:
                with fixture.client.session_transaction() as browser_session:
                    browser_session["user_id"] = switch_user_to
            response = fixture.client.post("/api/chat/message", json={
                "session_id": fixture.sid, "content": answer,
                "page_url": "https://optiwar.com/account/orders",
                "client_message_id": "rx-" + answer + "-" + fixture.sid,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(model.call_count, 2 if intervening else 0)
        fixture.rx_failed_document_ids = failed_document_ids
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

    def _assert_no_ticket_created(self, fixture, forwarded):
        self.assertEqual(forwarded, [])
        cur = fixture.db.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM chat_rx_photo_test_tickets "
            "WHERE session_id=%s", (fixture.sid,))
        self.assertEqual(cur.fetchone()["n"], 0)

    def _assert_rx_photo_ket_forward_escalated(self, fixture):
        cur = fixture.db.cursor()
        cur.execute(
            "SELECT action_type,payload FROM ai_events WHERE session_id=%s "
            "AND event_type=%s",
            (fixture.sid, self.cg.acr.EV_HANDOVER_ESCALATED))
        matches = []
        for event in cur.fetchall():
            payload = event["payload"]
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            if json.loads(payload or "{}").get("reason") == (
                    "rx_photo_ket_forward_failed"):
                matches.append(event)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["action_type"], "PHOTO_ESCALATION")

    def _temporary_rx_storage(self):
        storage = tempfile.TemporaryDirectory(prefix="chat-rx-docs-")
        upload_dir = mock.patch.object(self.ordx, "upload_dir",
                                       return_value=storage.name)
        upload_dir.start()
        self.addCleanup(storage.cleanup)
        self.addCleanup(upload_dir.stop)

    def _delete_chat_documents(self, db, attachment_id):
        cur = db.cursor()
        cur.execute(
            "SELECT document_id FROM order_rx_documents "
            "WHERE chat_attachment_id=%s", (attachment_id,))
        for row in cur.fetchall():
            cur.execute(
                "DELETE FROM order_rx_document_audit WHERE document_id=%s",
                (row["document_id"],))
        cur.execute("DELETE FROM order_rx_documents WHERE chat_attachment_id=%s",
                    (attachment_id,))

    def test_decline_message_turn_creates_ticket_and_forwards_photo(self):
        fixture, forwarded, body = self._ticket_turn("no")
        self.assertEqual(body["actions"], ["create_ticket"])
        self.assertEqual(body["rx_photo_action"]["state"], "DECLINED")
        self.assertIn("Your support ticket KET-31 has been created", body["reply"])
        self._assert_ticket_and_photo_were_forwarded(fixture, forwarded)

    def test_ticketed_decline_does_not_create_a_second_ticket(self):
        fixture, forwarded, body = self._ticket_turn("no", ticketed=True)
        self.assertEqual(body["actions"], [])
        self.assertEqual(body["rx_photo_action"]["state"], "DECLINED")
        self.assertEqual(
            body["reply"],
            "Okay — I haven't added it to an order. Our support team already "
            "has the photo on your ticket.")
        self.assertEqual(forwarded, [])
        cur = fixture.db.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM chat_rx_photo_test_tickets "
            "WHERE session_id=%s", (fixture.sid,))
        self.assertEqual(cur.fetchone()["n"], 0)
        self.assertEqual(len(fixture.uploads), 1)
        self.assertEqual(fixture.uploads[0][0], "uid-31")

    def test_ticketed_decline_retries_failed_forward_before_claiming_ticket_has_photo(self):
        fixture, forwarded, body = self._ticket_turn(
            "no", ticketed=True,
            ket_upload_answers=[(False, "http 503"), (True, "ref-31")])
        self.assertEqual(fixture.rx_offer["ket_status"], "failed")
        self.assertEqual(body["actions"], [])
        self.assertEqual(body["rx_photo_action"]["state"], "DECLINED")
        self.assertEqual(
            body["reply"],
            "Okay — I haven't added it to an order. Our support team already "
            "has the photo on your ticket.")
        self.assertEqual(len(fixture.uploads), 2)
        cur = fixture.db.cursor()
        cur.execute(
            "SELECT ket_status,ket_ticket_uid FROM chat_attachments WHERE id=%s",
            (fixture.rx_offer["attachment_id"],))
        self.assertEqual(cur.fetchone(), {
            "ket_status": "sent", "ket_ticket_uid": "uid-31"})
        self._assert_no_ticket_created(fixture, forwarded)

    def test_ticketed_decline_reports_failed_forward_without_creating_ticket(self):
        fixture, forwarded, body = self._ticket_turn(
            "no", ticketed=True,
            ket_upload_answers=[(False, "http 503"), (False, "http 503")])
        self.assertEqual(body["actions"], [])
        self.assertEqual(body["rx_photo_action"]["state"], "DECLINED")
        self.assertEqual(
            body["reply"],
            "Okay — I haven't added it to an order. I couldn't send the photo "
            "to your support ticket automatically — our team will see it in "
            "this chat, but please also reply to your ticket email with the photo.")
        self.assertEqual(len(fixture.uploads), 2)
        cur = fixture.db.cursor()
        cur.execute(
            "SELECT ket_status,ket_ticket_uid FROM chat_attachments WHERE id=%s",
            (fixture.rx_offer["attachment_id"],))
        self.assertEqual(cur.fetchone(), {
            "ket_status": "failed", "ket_ticket_uid": "uid-31"})
        self._assert_rx_photo_ket_forward_escalated(fixture)
        self._assert_no_ticket_created(fixture, forwarded)

    def test_ticketed_attach_failure_retries_failed_forward_before_claiming_ticket_has_photo(self):
        fixture, forwarded, body = self._ticket_turn(
            "yes", ticketed=True, attach_failure=True,
            ket_upload_answers=[(False, "http 503"), (True, "ref-31")])
        self.assertEqual(fixture.rx_offer["ket_status"], "failed")
        self.assertEqual(body["actions"], [])
        self.assertEqual(body["rx_photo_action"]["state"], "FAILED")
        self.assertEqual(
            body["reply"],
            "I couldn't add it to the order, so our support team already has "
            "the photo on your ticket.")
        self.assertEqual(len(fixture.uploads), 2)
        cur = fixture.db.cursor()
        cur.execute(
            "SELECT ket_status,ket_ticket_uid FROM chat_attachments WHERE id=%s",
            (fixture.rx_offer["attachment_id"],))
        self.assertEqual(cur.fetchone(), {
            "ket_status": "sent", "ket_ticket_uid": "uid-31"})
        self._assert_no_ticket_created(fixture, forwarded)

    def test_ticketed_attach_failure_reports_failed_forward_without_creating_ticket(self):
        fixture, forwarded, body = self._ticket_turn(
            "yes", ticketed=True, attach_failure=True,
            ket_upload_answers=[(False, "http 503"), (False, "http 503")])
        self.assertEqual(body["actions"], [])
        self.assertEqual(body["rx_photo_action"]["state"], "FAILED")
        self.assertEqual(
            body["reply"],
            "I couldn't send the photo to your support ticket automatically — "
            "our team will see it in this chat, but please also reply to your "
            "ticket email with the photo.")
        self.assertEqual(len(fixture.uploads), 2)
        cur = fixture.db.cursor()
        cur.execute(
            "SELECT ket_status,ket_ticket_uid FROM chat_attachments WHERE id=%s",
            (fixture.rx_offer["attachment_id"],))
        self.assertEqual(cur.fetchone(), {
            "ket_status": "failed", "ket_ticket_uid": "uid-31"})
        self._assert_rx_photo_ket_forward_escalated(fixture)
        self._assert_no_ticket_created(fixture, forwarded)

    def test_nonpreparing_target_failure_creates_ticket_and_forwards_photo(self):
        fixture, forwarded, body = self._ticket_turn("yes", tampered=True)
        self.assertEqual(body["actions"], ["create_ticket"])
        self.assertEqual(body["rx_photo_action"]["state"], "FAILED")
        self.assertEqual(body["rx_photo_action"]["code"], "not_preparing")
        self.assertIn("Your support ticket KET-31 has been created", body["reply"])
        self._assert_ticket_and_photo_were_forwarded(fixture, forwarded)

    def test_storage_failure_removes_document_and_opens_ticket(self):
        fixture, forwarded, body = self._ticket_turn("yes", attach_failure=True)
        self.assertEqual(body["actions"], ["create_ticket"])
        self.assertEqual(body["rx_photo_action"]["state"], "FAILED")
        self.assertIn("Your support ticket KET-31 has been created", body["reply"])
        self._assert_ticket_and_photo_were_forwarded(fixture, forwarded)
        self.assertEqual(len(fixture.rx_failed_document_ids), 1)
        document_id = fixture.rx_failed_document_ids[0]
        cur = fixture.db.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM order_rx_documents WHERE document_id=%s",
            (document_id,))
        self.assertEqual(cur.fetchone()["n"], 0)
        cur.execute(
            "SELECT COUNT(*) AS n FROM order_rx_document_audit WHERE document_id=%s",
            (document_id,))
        self.assertEqual(cur.fetchone()["n"], 0)

    def test_account_switch_before_confirmation_fails_and_falls_back(self):
        fixture, forwarded, body = self._ticket_turn(
            "yes", switch_user_to=202)
        self.assertEqual(body["actions"], ["create_ticket"])
        self.assertEqual(body["rx_photo_action"]["state"], "FAILED")
        self.assertEqual(body["rx_photo_action"]["code"], "account_changed")
        self._assert_ticket_and_photo_were_forwarded(fixture, forwarded)
        cur = fixture.db.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM order_rx_documents "
            "WHERE chat_attachment_id=%s", (fixture.rx_offer["attachment_id"],))
        self.assertEqual(cur.fetchone()["n"], 0)

    def test_unrelated_turn_supersedes_stale_photo_offer(self):
        fixture, _forwarded, body = self._ticket_turn(
            "yes", intervening=True)
        self.assertEqual(body["actions"], [])
        self.assertNotIn("rx_photo_action", body)
        cur = fixture.db.cursor()
        cur.execute(
            "SELECT status FROM ai_actions WHERE session_id=%s "
            "AND action_type='RX_PHOTO_ATTACH'", (fixture.sid,))
        self.assertEqual(cur.fetchone()["status"], "SUPERSEDED")
        cur.execute(
            "SELECT COUNT(*) AS n FROM order_rx_documents "
            "WHERE chat_attachment_id=%s", (fixture.rx_offer["attachment_id"],))
        self.assertEqual(cur.fetchone()["n"], 0)

    def test_repeat_photo_confirmation_defers_to_newer_offers(self):
        for face_pending, nav_pending, satisfaction_pending in (
                ({"action_id": 1}, None, None),
                (None, {"action_id": 2}, None),
                (None, None, {"intent": "support"})):
            with self.subTest(face=bool(face_pending), nav=bool(nav_pending),
                              satisfaction=bool(satisfaction_pending)):
                with (mock.patch.object(
                            self.cg.acr, "get_live_pending_action_of",
                            return_value=None),
                      mock.patch.object(
                            self.cg.face_assistant, "live_pending",
                            return_value=("FACE_SHOP_FOR", face_pending)),
                      mock.patch.object(
                            self.cg.acr, "get_live_pending_action",
                            return_value=nav_pending),
                      mock.patch.object(
                            self.cg.satisfaction, "pending",
                            return_value=satisfaction_pending),
                      mock.patch.object(
                            self.cg, "_assistant_reply_before",
                            return_value={
                                "metadata": {"trace": {"action": {
                                    "id": 7, "type": "RX_PHOTO_ATTACH",
                                    "state": "EXECUTED"}}}})):
                    result = self.cg._rx_photo_confirmation(
                        _ConfirmationDB(None), "rx-session",
                        {"customer_id": 101}, "yes", "/")
                self.assertEqual(result, (None, None, []))

    def test_successful_attach_preserves_typed_snapshot_and_repeat_yes(self):
        self._temporary_rx_storage()
        fixture, _forwarded, body = self._ticket_turn("yes")
        attachment_id = fixture.rx_offer["attachment_id"]
        self.addCleanup(self._delete_chat_documents, fixture.db, attachment_id)
        self.assertEqual(body["rx_photo_action"]["state"], "EXECUTED")
        cur = fixture.db.cursor()
        cur.execute(
            "SELECT document_id,order_id,order_rx_id,typed_json,comparison "
            "FROM order_rx_documents WHERE chat_attachment_id=%s",
            (attachment_id,))
        document = cur.fetchone()
        self.assertIsNotNone(document)
        self.assertEqual(document["order_id"], "OW-22")
        self.assertEqual(document["order_rx_id"], fixture.rx_id)
        cur.execute("SELECT right_eye,left_eye FROM rx_collector WHERE rx_id=%s",
                    (fixture.rx_id,))
        typed_from_rx = self._typed_from_rx_row(cur.fetchone())
        self.assertEqual(json.loads(document["typed_json"]), typed_from_rx)
        self.assertEqual(document["comparison"], "MATCH")

        with mock.patch.object(self.cg, "_call_deepseek") as model:
            repeated = fixture.client.post("/api/chat/message", json={
                "session_id": fixture.sid, "content": "yes",
                "page_url": "https://optiwar.com/account/orders",
                "client_message_id": "rx-repeat-" + fixture.sid,
            })
        self.assertEqual(repeated.status_code, 200)
        repeated_body = repeated.get_json()
        self.assertEqual(
            repeated_body["reply"],
            "That photo is already added to order OW-22 — nothing more to do.")
        self.assertEqual(repeated_body["actions"], [])
        self.assertEqual(repeated_body["rx_photo_action"]["state"], "SETTLED")
        self.assertEqual(repeated_body["rx_photo_action"]["code"],
                         "already_settled")
        model.assert_not_called()
        self.assertEqual(len(fixture.seen), 1)
        cur.execute(
            "SELECT COUNT(*) AS n FROM order_rx_documents "
            "WHERE chat_attachment_id=%s", (attachment_id,))
        self.assertEqual(cur.fetchone()["n"], 1)

    def test_different_photo_sphere_is_compared_with_typed_rx(self):
        self._temporary_rx_storage()
        proposal = {
            "right": {"sph": "+3.25", "cyl": "-0.50", "axis": "180",
                      "add": "1.50"},
            "left": {"sph": "-2.00", "cyl": "-0.25", "axis": "90",
                     "add": "1.50"},
        }
        fixture, _forwarded, _body = self._ticket_turn(
            "yes", vision_proposal=proposal)
        attachment_id = fixture.rx_offer["attachment_id"]
        self.addCleanup(self._delete_chat_documents, fixture.db, attachment_id)
        cur = fixture.db.cursor()
        cur.execute(
            "SELECT comparison,differences_json FROM order_rx_documents "
            "WHERE chat_attachment_id=%s", (attachment_id,))
        document = cur.fetchone()
        self.assertEqual(document["comparison"], "DIFFERENT")
        self.assertIn(
            "R SPH: photo +3.25, typed +1.25",
            json.loads(document["differences_json"]))

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
                "rx_id": 5, "product_name": "Frame",
                "typed": {"right_pwr": 1.25, "right_cyl": -0.5,
                          "right_axis": 180, "right_add": 1.5,
                          "left_pwr": -2.0, "left_cyl": -0.25,
                          "left_axis": 90, "left_add": 1.5}}
        assistant = {
            "id": 120,
            "content": "Shall I add this photo to order OW-22 (Frame)?",
            "metadata": json.dumps({"about_attachment_id": 19}),
        }
        db = _ConfirmationDB(attachment, assistant)
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
            with self.app.test_request_context():
                self.cg.flask_session["user_id"] = 101
                reply, result, actions = self.cg._rx_photo_confirmation(
                    db, "rx-session", {"customer_id": 101}, "yes",
                    "/account/orders")
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
        assistant = {
            "id": 121,
            "content": "Shall I add this photo to order OW-23 (Frame)?",
            "metadata": json.dumps({"about_attachment_id": 20}),
        }
        with (mock.patch.object(self.cg.acr, "get_live_pending_action_of",
                                return_value=pending),
              mock.patch.object(self.cg.acr, "mark_action", return_value=True) as mark):
            reply, result, actions = self.cg._rx_photo_confirmation(
                _ConfirmationDB(None, assistant), "rx-session",
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
            with self.app.test_request_context():
                self.cg.flask_session["user_id"] = 101
                reply, result, actions = self.cg._rx_photo_confirmation(
                    _ConfirmationDB(attachment, assistant), "rx-session",
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
