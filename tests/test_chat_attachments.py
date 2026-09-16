"""KET-IMG: a customer's photo goes widget -> Optiwar -> KET, never widget -> KET.

The bytes decide the type (not the name, not the declared MIME), 8 MB is the
ceiling, the file lives outside the web root under a name that carries nothing
of the customer, and the transcript holds only a marker. On the ticket's create
call the first four photos ride as KET's ``images[]`` (Option A); anything
after the ticket exists goes to ``/{ticket_uid}/attachments`` (Option B). The
KET key and the image bytes appear in no log line.

Unit tests need no database. ``OnMariaDB`` cases prove the routes against the
CI MariaDB and are skipped without one.
"""
import base64
import importlib.util
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import types
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

DB_CONF = dict(
    host=os.environ.get("OPTIWAR_TEST_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("OPTIWAR_TEST_MYSQL_PORT", "3306")),
    user=os.environ.get("OPTIWAR_TEST_MYSQL_USER", "oslb6"),
    password=os.environ.get("OPTIWAR_TEST_MYSQL_PASSWORD", "testpw"),
    database=os.environ.get("OPTIWAR_TEST_MYSQL_DB", "optiwar2"),
)


def _connect():
    import pymysql
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor,
                           autocommit=True, connect_timeout=5, **DB_CONF)


def _available():
    try:
        _connect().close()
        return True
    except Exception:  # noqa: BLE001
        return False


AVAILABLE = _available()


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ca = _load("chat_attachments_under_test", "chat_attachments.py")

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
GIF = b"GIF89a" + b"\x00" * 200
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 200
PDF = b"%PDF-1.4\n" + b"\x00" * 200


class Validation(unittest.TestCase):

    def test_the_four_formats_are_read_from_their_bytes(self):
        for data, kind, mime in ((JPEG, "jpeg", "image/jpeg"), (PNG, "png", "image/png"),
                                 (GIF, "gif", "image/gif"), (WEBP, "webp", "image/webp")):
            info = ca.validate(data, "anything.bin")
            self.assertEqual((info["kind"], info["mime_type"]), (kind, mime))
            self.assertEqual(info["byte_size"], len(data))
            self.assertEqual(len(info["sha256"]), 64)

    def test_the_name_does_not_decide_the_type(self):
        with self.assertRaises(ca.Rejected) as ctx:
            ca.validate(PDF, "photo.jpg")
        self.assertEqual(ctx.exception.code, "ATTACHMENT_TYPE")
        with self.assertRaises(ca.Rejected):
            ca.validate(b"<svg xmlns='http://www.w3.org/2000/svg'>" + b" " * 100, "x.svg")

    def test_size_bounds(self):
        with self.assertRaises(ca.Rejected) as ctx:
            ca.validate(b"", "a.jpg")
        self.assertEqual(ctx.exception.code, "ATTACHMENT_EMPTY")
        with self.assertRaises(ca.Rejected) as ctx:
            ca.validate(JPEG[:4] + b"\x00" * ca.MAX_BYTES, "a.jpg")
        self.assertEqual(ctx.exception.code, "ATTACHMENT_TOO_LARGE")
        self.assertEqual(ca.MAX_BYTES, 8 * 1024 * 1024)
        ca.validate(JPEG[:4] + b"\x00" * (ca.MAX_BYTES - 4), "exactly-8mb.jpg")

    def test_the_filename_is_a_label_stripped_of_paths_and_the_stored_name_is_not_it(self):
        info = ca.validate(JPEG, "../../etc/passwd")
        self.assertEqual(info["filename"], "passwd.jpg")
        info = ca.validate(PNG, "C:\\Users\\me\\My Photo<1>.PNG")
        self.assertEqual(info["filename"], "My Photo1.PNG")
        self.assertEqual(ca.validate(GIF, "")["filename"], "photo.gif")
        self.assertEqual(ca.stored_name(42, "jpeg"), "chatimg-42.jpg")
        self.assertEqual(ca.stored_name(7, "webp"), "chatimg-7.webp")

    def test_the_transcript_marker_and_ket_images_shape(self):
        self.assertEqual(ca.transcript_line("a.jpg"), "[Photo attached: a.jpg]")
        rows = [({"filename": "a.jpg", "mime_type": "image/jpeg"}, JPEG)]
        images = ca.ket_images(rows)
        self.assertEqual(images, [{"filename": "a.jpg", "mime_type": "image/jpeg",
                                   "data_base64": base64.b64encode(JPEG).decode("ascii")}])
        self.assertFalse(images[0]["data_base64"].startswith("data:"))
        self.assertEqual(ca.VISION_ANALYSED, 4)

    def test_the_schema_is_additive_and_declared_for_the_deploy_tool(self):
        self.assertEqual([n for n, _ in ca.TABLES], ["chat_attachments"])
        self.assertIn("CREATE TABLE IF NOT EXISTS chat_attachments", ca.SCHEMA)
        self.assertEqual(ca.SESSION_COLUMNS[0][0], "chat_sessions")
        self.assertEqual([n for n, _ in ca.SESSION_COLUMNS[0][1]],
                         ["ket_ticket_uid", "ket_ticket_ref"])


# --------------------------------------------------------------------------
# crm: Option A on create, Option B afterwards
# --------------------------------------------------------------------------
def _load_crm():
    pkg = types.ModuleType("flaskr_crm_test")
    pkg.__path__ = [REPO]
    sys.modules[pkg.__name__] = pkg
    for name, attrs in (("mail", dict(send_contact_email=lambda *a, **k: None,
                                      create_ticket_in_db=lambda *a, **k: None)),
                        ("captcha", dict(CaptchaGenerator=object))):
        m = types.ModuleType(pkg.__name__ + "." + name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[m.__name__] = m
    if "flask_mail" not in sys.modules:
        fm = types.ModuleType("flask_mail")
        fm.Message = object
        sys.modules["flask_mail"] = fm
    spec = importlib.util.spec_from_file_location(pkg.__name__ + ".crm",
                                                  os.path.join(REPO, "crm.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class KetForwarding(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.crm = _load_crm()

    def setUp(self):
        self.calls = []
        self.answers = []
        crm = self.crm

        def post(url, **kw):
            self.calls.append((url, kw))
            return self.answers.pop(0)

        self._post, crm.requests.post = crm.requests.post, post
        self._sleep, crm.time.sleep = crm.time.sleep, lambda s: None
        self._site, crm._get_site_from = crm._get_site_from, lambda: "optiwar.com"
        os.environ["KET_SUPPORT_KEY_OPTIWAR"] = "SECRET-KEY-OPTIWAR"
        self.log = io.StringIO()
        self.handler = logging.StreamHandler(self.log)
        logging.getLogger().addHandler(self.handler)
        self._level = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)

    def tearDown(self):
        crm = self.crm
        crm.requests.post = self._post
        crm.time.sleep = self._sleep
        crm._get_site_from = self._site
        logging.getLogger().removeHandler(self.handler)
        logging.getLogger().setLevel(self._level)
        os.environ.pop("KET_SUPPORT_KEY_OPTIWAR", None)

    def _images(self):
        return ca.ket_images([({"filename": "crack.jpg", "mime_type": "image/jpeg"}, JPEG)])

    def test_option_a_sends_images_in_the_create_json_and_returns_ref_and_uid(self):
        self.answers.append(_Resp(201, {"ticket_id": "KET-1001", "ticket_uid": "uid-abc"}))
        out = self.crm._forward_to_ket("Jane", "jane@example.com", "", "Cracked lens",
                                       "see photo", source="ai_chat_handover",
                                       session_id="s1", images=self._images())
        self.assertEqual(out, {"ticket_id": "KET-1001", "ticket_ref": "KET-1001",
                               "ticket_uid": "uid-abc"})
        url, kw = self.calls[0]
        self.assertEqual(url, self.crm.KET_API_URL)
        self.assertEqual(kw["headers"]["X-API-Key"], "SECRET-KEY-OPTIWAR")
        self.assertEqual(kw["json"]["source"], "ai_chat")
        self.assertEqual(kw["json"]["images"], self._images())
        self.assertEqual(kw["timeout"], 30)

    def test_no_images_means_no_images_key_and_the_old_timeout(self):
        self.answers.append(_Resp(200, {"ticket_id": "KET-1"}))
        out = self.crm._forward_to_ket("J", "j@example.com", "", "s", "d", images=None)
        self.assertNotIn("images", self.calls[0][1]["json"])
        self.assertEqual(self.calls[0][1]["timeout"], 15)
        self.assertIsNone(out["ticket_uid"])

    def test_create_retries_once_on_5xx_and_not_on_4xx(self):
        self.answers += [_Resp(503), _Resp(201, {"ticket_id": "K", "uid": "u2"})]
        out = self.crm._forward_to_ket("J", "j@example.com", "", "s", "d", images=self._images())
        self.assertEqual(out["ticket_uid"], "u2")
        self.assertEqual(len(self.calls), 2)
        self.calls[:] = []
        self.answers.append(_Resp(413, {"error": "too large"}))
        self.assertIsNone(self.crm._forward_to_ket("J", "j@example.com", "", "s", "d",
                                                   images=self._images()))
        self.assertEqual(len(self.calls), 1)

    def test_option_b_posts_multipart_file_to_the_ticket_uid(self):
        self.answers.append(_Resp(201, {"attachment_id": 77}))
        ok, ref = self.crm.ket_attachment_upload("uid-abc", "crack.jpg", "image/jpeg", JPEG)
        self.assertEqual((ok, ref), (True, "77"))
        url, kw = self.calls[0]
        self.assertEqual(url, self.crm.KET_API_URL + "/uid-abc/attachments")
        self.assertEqual(kw["headers"], {"X-API-Key": "SECRET-KEY-OPTIWAR"})
        self.assertEqual(kw["files"]["file"], ("crack.jpg", JPEG, "image/jpeg"))
        self.assertNotIn("json", kw)

    def test_option_b_retries_5xx_once_refuses_4xx_and_needs_a_uid(self):
        self.answers += [_Resp(500), _Resp(200, {})]
        self.assertEqual(self.crm.ket_attachment_upload("u", "a.jpg", "image/jpeg", JPEG),
                         (True, ""))
        self.assertEqual(len(self.calls), 2)
        self.calls[:] = []
        self.answers.append(_Resp(415))
        ok, why = self.crm.ket_attachment_upload("u", "a.jpg", "image/jpeg", JPEG)
        self.assertEqual((ok, why), (False, "http 415"))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.crm.ket_attachment_upload("", "a.jpg", "image/jpeg", JPEG),
                         (False, "no ticket_uid"))
        os.environ["KET_SUPPORT_KEY_OPTIWAR"] = ""
        self.assertFalse(self.crm.ket_attachment_upload("u", "a.jpg", "image/jpeg", JPEG)[0])

    def test_neither_the_key_nor_the_bytes_reach_the_log(self):
        self.answers += [_Resp(201, {"ticket_id": "K", "ticket_uid": "u"}), _Resp(500), _Resp(502)]
        self.crm._forward_to_ket("J", "j@example.com", "", "s", "d", images=self._images())
        self.crm.ket_attachment_upload("u", "crack.jpg", "image/jpeg", JPEG)
        text = self.log.getvalue()
        self.assertIn("images=1", text)
        self.assertNotIn("SECRET-KEY-OPTIWAR", text)
        self.assertNotIn(base64.b64encode(JPEG).decode("ascii")[:24], text)
        self.assertNotIn("data_base64", text)


# --------------------------------------------------------------------------
# chat_gateway routes on MariaDB
# --------------------------------------------------------------------------
CHAT_SESSIONS_DDL = """CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id VARCHAR(64) PRIMARY KEY,
    status VARCHAR(24) NOT NULL DEFAULT 'active',
    contact_name VARCHAR(191) NULL, contact_email VARCHAR(191) NULL,
    last_activity DATETIME NULL, created_at DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

CHAT_MESSAGES_DDL = """CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGINT AUTO_INCREMENT PRIMARY KEY, session_id VARCHAR(64) NULL,
    source VARCHAR(24) NULL, role VARCHAR(24) NULL, content MEDIUMTEXT NULL,
    status VARCHAR(24) NULL, metadata TEXT NULL, client_message_id VARCHAR(64) NULL,
    created_at DATETIME NULL, KEY idx_session (session_id, created_at),
    UNIQUE KEY uq_client (session_id, source, client_message_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def _load_gateway():
    for name in ("openai", "httpx"):
        if name not in sys.modules:
            m = types.ModuleType(name)
            if name == "openai":
                m.OpenAI = type("OpenAI", (), {"__init__": lambda self, *a, **k: None})
                for n in ("APIConnectionError", "APITimeoutError", "InternalServerError",
                          "RateLimitError"):
                    setattr(m, n, type(n, (Exception,), {}))
            else:
                m.Timeout = type("Timeout", (), {"__init__": lambda self, *a, **k: None})
            sys.modules[name] = m
    pkg = types.ModuleType("flaskr")
    pkg.__path__ = [REPO]
    sys.modules["flaskr"] = pkg
    mail = types.ModuleType("flaskr.mail")
    mail.create_ticket_in_db = lambda *a, **k: None
    mail.send_contact_email = lambda *a, **k: None
    sys.modules["flaskr.mail"] = mail
    cap = types.ModuleType("flaskr.captcha")
    cap.CaptchaGenerator = object
    sys.modules["flaskr.captcha"] = cap
    _load("flaskr.ai_client", "ai_client.py")
    return _load("flaskr.chat_gateway", "chat_gateway.py")


@unittest.skipUnless(AVAILABLE, "no MariaDB test database (see scripts/setup_test_db.sh)")
class OnMariaDB(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Flask
        cls.cg = _load_gateway()
        cls.db = _connect()
        cur = cls.db.cursor()
        cur.execute(CHAT_SESSIONS_DDL)
        cur.execute(CHAT_MESSAGES_DDL)
        # Another suite may have created chat_messages first, without the
        # idempotency column production has; bring it up to that shape.
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                    "AND TABLE_NAME='chat_messages' AND COLUMN_NAME='client_message_id'")
        if not cur.fetchone()["n"]:
            cur.execute("ALTER TABLE chat_messages ADD COLUMN client_message_id VARCHAR(64) NULL, "
                        "ADD UNIQUE KEY uq_client (session_id, source, client_message_id)")
        cls.cg.chat_attachments.ensure_schema(cur)
        cls.cg.chat_attachments.ensure_schema(cur)  # idempotent
        cls.cg._get_db = staticmethod(_connect)
        cls.tmp = tempfile.mkdtemp(prefix="owchat")
        os.makedirs(os.path.join(cls.tmp, "app"))
        cls.app = Flask(__name__, root_path=os.path.join(cls.tmp, "app"))
        cls.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        cls.app.register_blueprint(cls.cg.bp)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.sid = "att_" + uuid.uuid4().hex[:12]
        self.db.cursor().execute(
            "INSERT INTO chat_sessions (session_id, status, contact_name, contact_email, "
            "created_at, last_activity) VALUES (%s, 'active', 'Jane', 'jane@example.com', NOW(), NOW())",
            (self.sid,))
        self.uploads = []
        self.answers = []
        cg = self.cg

        def fake_upload(ticket_uid, filename, mime_type, data):
            self.uploads.append((ticket_uid, filename, mime_type, data))
            return self.answers.pop(0) if self.answers else (True, "ref-1")

        crm = sys.modules.get("flaskr.crm")
        if crm is None:
            crm = _load("flaskr.crm", "crm.py")
        self._real_upload = crm.ket_attachment_upload
        crm.ket_attachment_upload = fake_upload
        self.crm = crm
        # The vision model, scripted: by default it sees a frame it is sure of.
        self.reading = cg.chat_vision.Reading(
            kind="frame", description="A pair of spectacles on a table.",
            frame={"colour": "black", "shape": "rectangular"}, pd="", proposal=None,
            confidence=0.9, unreadable=False)
        self.seen = []

        def fake_describe(data, mime_type, endpoint="/api/chat/attachment"):
            self.seen.append((mime_type, data))
            if isinstance(self.reading, Exception):
                raise self.reading
            return self.reading, "scripted-vision"

        self._real_describe = cg.chat_vision.describe
        cg.chat_vision.describe = fake_describe
        self.client.delete_cookie("ow_chat_token")
        with self.app.test_request_context():
            self.token = cg._chat_cookie_serializer().dumps(self.sid)

    def tearDown(self):
        self.crm.ket_attachment_upload = self._real_upload
        self.cg.chat_vision.describe = self._real_describe
        cur = self.db.cursor()
        for t in ("chat_attachments", "chat_messages", "chat_sessions"):
            cur.execute("DELETE FROM %s WHERE session_id=%%s" % t, (self.sid,))

    def _as_owner(self, sid=None):
        with self.app.test_request_context():
            self.client.set_cookie("ow_chat_token", self.cg._chat_cookie_serializer().dumps(sid or self.sid))

    def _post(self, data=JPEG, name="crack.jpg", mime="image/jpeg", sid=None):
        return self.client.post(
            "/api/chat/attachment",
            data={"session_id": sid or self.sid, "file": (io.BytesIO(data), name, mime)},
            content_type="multipart/form-data")

    def _rows(self):
        cur = self.db.cursor()
        cur.execute("SELECT * FROM chat_attachments WHERE session_id=%s ORDER BY id", (self.sid,))
        return cur.fetchall()

    def test_a_stranger_gets_403_and_nothing_is_stored(self):
        r = self._post()
        self.assertEqual(r.status_code, 403)
        self._as_owner("someone_else")
        self.assertEqual(self._post().status_code, 403)
        self.assertEqual(self._rows(), ())

    def test_a_photo_is_stored_privately_marked_in_the_transcript_and_pending_for_ket(self):
        self._as_owner()
        r = self._post(PNG, name="../secret/photo one.png", mime="image/jpeg")
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertEqual(j["mime_type"], "image/png")        # bytes, not declared MIME
        self.assertEqual(j["filename"], "photo one.png")
        self.assertEqual(j["ket_status"], "pending")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["stored_name"], "chatimg-%d.png" % j["attachment_id"])
        path = os.path.join(self.tmp, "secure_uploads", "chat", row["stored_name"])
        self.assertTrue(os.path.exists(path))
        self.assertNotIn("static", path)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), PNG)
        cur = self.db.cursor()
        cur.execute("SELECT content, metadata, source FROM chat_messages WHERE id=%s", (j["message_id"],))
        m = cur.fetchone()
        self.assertEqual(m["content"], "[Photo attached: photo one.png]")
        self.assertEqual(json.loads(m["metadata"]), {"attachment_id": j["attachment_id"]})
        self.assertEqual(m["source"], "customer")
        self.assertEqual(self.uploads, [])   # no ticket yet -> nothing sent

        listing = self.client.get("/api/chat/messages/%s" % self.sid).get_json()
        self.assertEqual(listing["messages"][0]["attachment_url"], "/api/chat/attachment/%d" % j["attachment_id"])
        got = self.client.get(j["url"])
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.data, PNG)
        self.assertEqual(got.mimetype, "image/png")
        self.assertEqual(got.headers["X-Content-Type-Options"], "nosniff")
        self.client.delete_cookie("ow_chat_token")
        self.assertEqual(self.client.get(j["url"]).status_code, 403)
        self.assertEqual(self.client.get("/api/chat/attachment/999999999").status_code, 404)

    def test_refusals_by_bytes_size_and_count_store_nothing(self):
        self._as_owner()
        r = self._post(PDF, name="photo.jpg")
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "ATTACHMENT_TYPE"))
        r = self._post(JPEG[:4] + b"\x00" * ca.MAX_BYTES)
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "ATTACHMENT_TOO_LARGE"))
        r = self.client.post("/api/chat/attachment", data={"session_id": self.sid},
                             content_type="multipart/form-data")
        self.assertEqual(r.get_json()["error"]["code"], "ATTACHMENT_MISSING")
        self.assertEqual(self._rows(), ())
        for i in range(ca.MAX_PER_SESSION):
            self.assertEqual(self._post(GIF, name="g%d.gif" % i).status_code, 200)
        r = self._post(GIF, name="one-too-many.gif")
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "ATTACHMENT_LIMIT"))
        self.assertEqual(len(self._rows()), ca.MAX_PER_SESSION)

    def test_after_the_ticket_exists_a_photo_goes_by_option_b_to_the_stored_uid(self):
        self.db.cursor().execute(
            "UPDATE chat_sessions SET ket_ticket_uid='uid-xyz', ket_ticket_ref='KET-9' WHERE session_id=%s",
            (self.sid,))
        self._as_owner()
        r = self._post(WEBP, name="later.webp", mime="image/webp")
        self.assertEqual(r.get_json()["ket_status"], "sent")
        self.assertEqual(self.uploads, [("uid-xyz", "later.webp", "image/webp", WEBP)])
        row = self._rows()[0]
        self.assertEqual((row["ket_status"], row["ket_via"], row["ket_ticket_uid"]),
                         ("sent", "attachments", "uid-xyz"))
        self.assertIsNotNone(row["ket_sent_at"])
        self.answers.append((False, "http 503"))
        r = self._post(WEBP, name="fail.webp", mime="image/webp")
        self.assertEqual(r.status_code, 200)             # the customer's chat is not broken
        self.assertEqual(r.get_json()["ket_status"], "failed")
        row = self._rows()[1]
        self.assertEqual((row["ket_status"], row["ket_error"]), ("failed", "http 503"))

    def test_handover_sends_the_first_four_on_create_the_rest_by_option_b_and_tells_the_ref(self):
        self._as_owner()
        for i in range(6):
            self.assertEqual(self._post(JPEG, name="p%d.jpg" % i).status_code, 200)
        cg = self.cg
        forwarded = []

        def fake_forward(**kw):
            forwarded.append(kw)
            return {"ticket_id": "KET-77", "ticket_ref": "KET-77", "ticket_uid": "uid-77"}

        mapped = []
        self.crm._forward_to_ket, real_fwd = fake_forward, self.crm._forward_to_ket
        self.crm.persist_ticket_mapping, real_map = (lambda *a, **k: mapped.append((a, k))), \
            self.crm.persist_ticket_mapping
        cg._generate_chat_summary, real_sum = (lambda db, sid: "summary"), cg._generate_chat_summary
        cg._send_fallback_email, real_mail = (lambda *a, **k: True), cg._send_fallback_email
        try:
            with self.app.test_request_context():
                db = _connect()
                cur = db.cursor()
                cur.execute("SELECT * FROM chat_sessions WHERE session_id=%s", (self.sid,))
                local_id, ket_ref = cg._forward_ticket_from_chat(db, self.sid, cur.fetchone(), "/x")
        finally:
            self.crm._forward_to_ket = real_fwd
            self.crm.persist_ticket_mapping = real_map
            cg._generate_chat_summary = real_sum
            cg._send_fallback_email = real_mail
        self.assertEqual(ket_ref, "KET-77")             # a string, not the dict
        self.assertEqual(len(forwarded), 1)
        images = forwarded[0]["images"]
        self.assertEqual([i["filename"] for i in images], ["p0.jpg", "p1.jpg", "p2.jpg", "p3.jpg"])
        self.assertEqual(images[0]["data_base64"], base64.b64encode(JPEG).decode("ascii"))
        self.assertEqual([u[1] for u in self.uploads], ["p4.jpg", "p5.jpg"])
        self.assertTrue(all(u[0] == "uid-77" for u in self.uploads))
        rows = self._rows()
        self.assertEqual([(r["ket_status"], r["ket_via"], r["ket_ticket_uid"]) for r in rows],
                         [("sent", "create", "uid-77")] * 4 + [("sent", "attachments", "uid-77")] * 2)
        cur = self.db.cursor()
        cur.execute("SELECT ket_ticket_uid, ket_ticket_ref FROM chat_sessions WHERE session_id=%s", (self.sid,))
        self.assertEqual(cur.fetchone(), {"ket_ticket_uid": "uid-77", "ket_ticket_ref": "KET-77"})
        transcript = json.loads(forwarded[0]["chat_transcript"])
        self.assertIn({"role": "user", "content": "[Photo attached: p0.jpg]"}, transcript)

    def test_a_create_without_a_uid_still_tells_the_ref_and_a_failed_create_marks_photos_failed(self):
        self._as_owner()
        self._post(JPEG, name="only.jpg")
        cg = self.cg
        results = [{"ticket_id": "KET-5", "ticket_ref": "KET-5", "ticket_uid": None}, None]
        self.crm._forward_to_ket, real_fwd = (lambda **kw: results.pop(0)), self.crm._forward_to_ket
        cg._generate_chat_summary, real_sum = (lambda db, sid: "summary"), cg._generate_chat_summary
        cg._send_fallback_email, real_mail = (lambda *a, **k: True), cg._send_fallback_email
        try:
            with self.app.test_request_context():
                db = _connect()
                cur = db.cursor()
                cur.execute("SELECT * FROM chat_sessions WHERE session_id=%s", (self.sid,))
                sess = cur.fetchone()
                _local, ref = cg._forward_ticket_from_chat(db, self.sid, sess, "/x")
                self.assertEqual(ref, "KET-5")
                row = self._rows()[0]
                self.assertEqual((row["ket_status"], row["ket_via"], row["ket_ticket_uid"]),
                                 ("sent", "create", None))
                # a second photo now: no uid -> stays pending, chat unaffected
                r = self._post(GIF, name="second.gif")
                self.assertEqual(r.get_json()["ket_status"], "pending")
                self.assertEqual(self.uploads, [])
                _local, ref = cg._forward_ticket_from_chat(db, self.sid, sess, "/x")
                self.assertIsNone(ref)
                self.assertEqual(self._rows()[1]["ket_status"], "failed")
                self.assertEqual(self._rows()[1]["ket_error"], "create failed")
        finally:
            self.crm._forward_to_ket = real_fwd
            cg._generate_chat_summary = real_sum
            cg._send_fallback_email = real_mail

    # ── the assistant looks at the photo ──────────────────────────────────

    def _ticket_flow(self):
        """Script the KET create call + summary + mail; returns the create-call list."""
        cg, forwarded = self.cg, []

        def fake_forward(**kw):
            forwarded.append(kw)
            return {"ticket_id": "KET-31", "ticket_ref": "KET-31", "ticket_uid": "uid-31"}
        self._saved = (self.crm._forward_to_ket, self.crm.persist_ticket_mapping,
                       cg._generate_chat_summary, cg._send_fallback_email, cg._send_ticket_email)
        self.crm._forward_to_ket = fake_forward
        self.crm.persist_ticket_mapping = lambda *a, **k: None
        cg._generate_chat_summary = lambda db, sid: "summary"
        cg._send_fallback_email = lambda *a, **k: True
        cg._send_ticket_email = lambda **k: None
        self.addCleanup(self._restore_ticket_flow)
        return forwarded

    def _restore_ticket_flow(self):
        (self.crm._forward_to_ket, self.crm.persist_ticket_mapping, self.cg._generate_chat_summary,
         self.cg._send_fallback_email, self.cg._send_ticket_email) = self._saved

    def _ai_messages(self):
        cur = self.db.cursor()
        cur.execute("SELECT content, metadata FROM chat_messages WHERE session_id=%s AND source='ai' ORDER BY id",
                    (self.sid,))
        return cur.fetchall()

    def test_a_frame_photo_is_described_back_from_the_vision_reading_not_a_canned_line(self):
        self._as_owner()
        forwarded = self._ticket_flow()
        r = self._post(JPEG, name="mine.jpg")
        j = r.get_json()
        self.assertEqual(self.seen, [("image/jpeg", JPEG)])       # the bytes went to the model
        self.assertIn("This looks like a black rectangular frame", j["reply"])
        self.assertNotIn("Tell me what I", j["reply"])
        self.assertEqual(j["actions"], [])
        self.assertEqual(forwarded, [])                            # sure -> no ticket
        row = self._rows()[0]
        self.assertEqual(row["vision_model"], "scripted-vision")
        self.assertEqual(json.loads(row["vision_json"])["kind"], "frame")
        self.assertIsNone(row["vision_error"])
        ai = self._ai_messages()
        self.assertEqual(len(ai), 1)
        self.assertEqual(ai[0]["content"], j["reply"])
        # The listing shows the reply as text, not as a second photo.
        listing = self.client.get("/api/chat/messages/%s" % self.sid).get_json()["messages"]
        self.assertEqual([m.get("attachment_url") for m in listing],
                         ["/api/chat/attachment/%d" % j["attachment_id"], None])
        # The text model is told what the photo showed, so it cannot claim blindness.
        with self.app.test_request_context():
            section = self.cg._photo_context(_connect(), self.sid)
        self.assertIn("never say you cannot view images", section)
        self.assertIn("mine.jpg (frame, confidence 0.9): A pair of spectacles", section)

    def test_a_prescription_photo_reads_back_per_eye_values_as_a_proposal(self):
        self._as_owner()
        self._ticket_flow()
        self.reading = self.cg.chat_vision.parse(json.dumps({
            "kind": "prescription", "description": "A printed spectacle prescription.",
            "prescription": {"right": {"sph": "-3.75", "cyl": "-0.75", "axis": "180", "add": "", "pd": "31"},
                             "left": {"sph": "-3.50", "cyl": "", "axis": "", "add": "+1.00", "pd": "32"},
                             "pd": "63"},
            "confidence": 0.85, "unreadable": False}))
        j = self._post(PNG, name="rx.png").get_json()
        self.assertIn("Right eye (OD): SPH -3.75, CYL -0.75, AXIS 180", j["reply"])
        self.assertIn("Left eye (OS): SPH -3.50, ADD 1.00", j["reply"])
        self.assertIn("PD: 63", j["reply"])
        self.assertIn("Nothing is applied until you confirm", j["reply"])
        self.assertEqual(j["actions"], [])
        self.assertNotIn("lens_rx_proposal", j)                  # no lens page -> nothing parked
        self.assertNotIn("[LENS_RX", j["reply"])

    def test_an_unsure_reading_escalates_by_itself_and_the_photo_rides_on_the_ticket(self):
        self._as_owner()
        forwarded = self._ticket_flow()
        self.reading = self.cg.chat_vision.parse(
            '{"kind": "other", "description": "Something blurred, possibly a receipt.", '
            '"confidence": 0.3, "unreadable": false}')
        j = self._post(GIF, name="blur.gif").get_json()
        self.assertEqual(j["actions"], ["create_ticket"])
        self.assertIn("I can see Something blurred, possibly a receipt.", j["reply"])
        self.assertIn("passed it \u2014 photo included \u2014 to our support team", j["reply"])
        self.assertIn("Your support ticket KET-31 has been created", j["reply"])
        self.assertEqual(len(forwarded), 1)
        self.assertEqual([i["filename"] for i in forwarded[0]["images"]], ["blur.gif"])  # Option A
        self.assertEqual(forwarded[0]["images"][0]["data_base64"], base64.b64encode(GIF).decode("ascii"))
        self.assertEqual(j["ket_status"], "sent")
        row = self._rows()[0]
        self.assertEqual((row["ket_status"], row["ket_via"], row["ket_ticket_uid"]), ("sent", "create", "uid-31"))
        cur = self.db.cursor()
        cur.execute("SELECT ket_ticket_uid FROM chat_sessions WHERE session_id=%s", (self.sid,))
        self.assertEqual(cur.fetchone()["ket_ticket_uid"], "uid-31")
        self.assertEqual(self._ai_messages()[0]["content"], j["reply"])   # stored with the ref

    def test_unreadable_and_a_prescription_without_values_both_escalate(self):
        self._as_owner()
        forwarded = self._ticket_flow()
        self.reading = self.cg.chat_vision.parse("I'm sorry, I can't tell what this is.")   # no JSON at all
        j = self._post(JPEG, name="dark.jpg").get_json()
        self.assertEqual(j["actions"], ["create_ticket"])
        self.assertIn("can't make it out well enough", j["reply"])
        self.assertEqual(len(forwarded), 1)
        # The conversation is a ticket now: the next unsure photo goes by Option B, no second ticket.
        self.reading = self.cg.chat_vision.parse(
            '{"kind": "prescription", "description": "A prescription, handwriting illegible.", '
            '"prescription": {}, "confidence": 0.9, "unreadable": false}')
        j2 = self._post(PNG, name="rx2.png").get_json()
        self.assertEqual(j2["actions"], [])
        self.assertIn("couldn't make out the values clearly, and it's on your open ticket", j2["reply"])
        self.assertEqual(len(forwarded), 1)
        self.assertEqual([u[:2] for u in self.uploads], [("uid-31", "rx2.png")])
        self.assertEqual(j2["ket_status"], "sent")

    def test_a_provider_failure_is_not_a_500_does_not_invent_a_description_and_escalates(self):
        self._as_owner()
        forwarded = self._ticket_flow()
        self.reading = self.cg.chat_vision.ai_client.ModelError("upstream 502")
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            r = self._post(WEBP, name="p.webp", mime="image/webp")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertIn("couldn't analyse that photo just now", j["reply"])
        self.assertEqual(j["actions"], ["create_ticket"])
        self.assertEqual(len(forwarded), 1)
        row = self._rows()[0]
        self.assertIsNone(row["vision_json"])
        self.assertEqual(row["vision_error"], "upstream 502")
        joined = "\n".join(logs.output)
        self.assertNotIn(base64.b64encode(WEBP).decode("ascii"), joined)
        self.assertNotIn(repr(WEBP), joined)

    def test_when_a_person_is_answering_the_assistant_only_hands_the_photo_on(self):
        self.db.cursor().execute(
            "UPDATE chat_sessions SET status='human_open', ket_ticket_uid='uid-h', ket_ticket_ref='KET-H' "
            "WHERE session_id=%s", (self.sid,))
        self._as_owner()
        j = self._post(JPEG, name="h.jpg").get_json()
        self.assertEqual(self.seen, [])
        self.assertEqual(j["reply"], "Your photo has been sent to my supervisor.")
        self.assertEqual(j["ket_status"], "sent")
        self.assertEqual([u[:2] for u in self.uploads], [("uid-h", "h.jpg")])
        cur = self.db.cursor()
        cur.execute("SELECT status FROM chat_sessions WHERE session_id=%s", (self.sid,))
        self.assertEqual(cur.fetchone()["status"], "human_open")


class VisionReading(unittest.TestCase):
    """The parser and the customer wording, no database."""

    @classmethod
    def setUpClass(cls):
        cls.cv = _load_gateway().chat_vision

    def test_confidence_rules(self):
        cv = self.cv
        sure = cv.parse('{"kind":"frame","description":"x","confidence":0.75}')
        self.assertTrue(sure.confident)
        self.assertFalse(cv.parse('{"kind":"frame","description":"x","confidence":0.5}').confident)
        self.assertFalse(cv.parse('{"kind":"other","description":"a cat","confidence":0.99}').confident)
        self.assertFalse(cv.parse('{"kind":"frame","description":"x","confidence":0.9,"unreadable":true}').confident)
        self.assertFalse(cv.parse('{"kind":"prescription","confidence":0.9,"prescription":{}}').confident)
        self.assertFalse(cv.parse('{"kind":"frame","description":"x"}').confident)      # no confidence given
        self.assertEqual(cv.parse("```json\n{\"kind\":\"frame\",\"confidence\":\"1.4\"}\n```").confidence, 1.0)
        bad = cv.parse("nonsense")
        self.assertTrue(bad.unreadable)
        self.assertEqual(bad.kind, "other")

    def test_prescription_values_are_canonical_and_pd_is_kept(self):
        r = self.cv.parse(json.dumps({"kind": "prescription", "confidence": 0.9, "prescription": {
            "right": {"sph": "-3.75", "cyl": "-0.75", "axis": "180", "pd": "31"},
            "left": {"sph": "plano"}, "pd": "63"}}))
        self.assertEqual(r.proposal["right"]["sph"], "-3.75")
        self.assertEqual(r.proposal["right"]["axis"], "180")
        self.assertEqual(r["pd"], "63")
        self.assertTrue(r.confident)
        text = self.cv.customer_reply(r)
        self.assertIn("Right eye (OD): SPH -3.75, CYL -0.75, AXIS 180", text)
        self.assertIn("PD: 63", text)

    def test_the_request_carries_the_image_as_a_data_url_and_the_prompt_asks_per_eye(self):
        msgs = self.cv.messages_for(b"\xff\xd8bytes", "image/jpeg")
        parts = msgs[0]["content"]
        self.assertEqual(parts[0]["type"], "text")
        for word in ("SPH", "CYL", "AXIS", "ADD", "PD", "colour", "shape", "unreadable"):
            self.assertIn(word, parts[0]["text"])
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertIn("frame", self.cv.prompt_section([("a.jpg", self.cv.parse(
            '{"kind":"frame","description":"Round tortoiseshell frame.","confidence":0.8}'))]))
        self.assertIn("could not read it", self.cv.prompt_section([("b.jpg", self.cv.parse("??"))]))
        self.assertIn("not analysed", self.cv.prompt_section([("c.jpg", None)]))
        self.assertEqual(self.cv.prompt_section([]), "")


if __name__ == "__main__":
    unittest.main()
