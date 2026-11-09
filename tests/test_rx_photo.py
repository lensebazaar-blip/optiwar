"""The eyeglasses prescription photo reader on the frames lens page: the
photo is normalised in memory, read by the configured vision workload with a
spectacle-only prompt, snapped to the page's own dropdown steps, and proposed.
Nothing is stored; reads are capped per session; no sign-in is needed.

Unit tests need no database.
"""
import datetime
import importlib.util
import io
import json
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _jpeg(w=1200, h=900):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (w, h), (250, 250, 250)).save(out, format="JPEG", quality=92)
    return out.getvalue()


class _QuotaCursor:
    """The statements the reader's guards run, over dicts; raises when told to."""
    counts = {}
    flags = []
    settings = {}
    broken = [False]
    _row = None

    def execute(self, sql, args=None):
        if self.broken[0]:
            raise RuntimeError("database is away")
        self._row = None
        if sql.startswith("INSERT INTO rx_photo_quota"):
            key = (args[0], args[1])
            self.counts[key] = self.counts.get(key, 0) + 1
        elif sql.startswith("SELECT read_count"):
            self._row = {"read_count": self.counts[(args[0], args[1])]}
        elif sql.startswith("INSERT INTO upload_flags"):
            self.flags.append(args)
        elif "FROM upload_flags" in sql:
            self._row = {"n": sum(1 for f in self.flags if f[3] == args[0])}
        elif "FROM ops_settings" in sql and args[0] in self.settings:
            self._row = {"setting_value": self.settings[args[0]]}

    def fetchone(self):
        return self._row


class _QuotaDb:
    def cursor(self):
        return _QuotaCursor()

    def commit(self):
        pass


def _load(call_model):
    """rx_photo with the model call faked, inside a throwaway package."""
    pkg_name = "rxp_pkg"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [REPO]
    sys.modules[pkg_name] = pkg

    def sub(name, **attrs):
        mod = types.ModuleType(pkg_name + "." + name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[pkg_name + "." + name] = mod
        setattr(pkg, name, mod)
        return mod

    def real(name):
        spec = importlib.util.spec_from_file_location(
            pkg_name + "." + name, os.path.join(REPO, "%s.py" % name))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        setattr(pkg, name, mod)
        spec.loader.exec_module(mod)
        return mod

    class ModelError(Exception):
        pass

    sub("ai_client", ModelError=ModelError, call_model=call_model)
    sub("db", get_db=_QuotaDb)
    sub("captcha", CaptchaGenerator=lambda: types.SimpleNamespace(
        generate_captcha=lambda: "AB12", generate_captcha_image=lambda t: "IMG"))
    real("content_guard")
    real("rx_photo_quota")
    real("ops_controls")
    real("dev_defects")
    real("lens_order")
    real("lens_rx")
    real("lens_documents")
    return real("rx_photo"), ModelError


class Snapping(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rx, _ = _load(lambda **kw: None)

    def test_values_are_read_from_what_the_model_wrote(self):
        n = self.rx._number
        self.assertEqual(n("-3.75"), -3.75)
        self.assertEqual(n("+1,25"), 1.25)
        self.assertEqual(n("PL"), 0.0)
        self.assertEqual(n("plano"), 0.0)
        self.assertEqual(n("DS"), 0.0)
        self.assertIsNone(n(""))
        self.assertIsNone(n(None))
        self.assertIsNone(n("n/a"))

    def test_powers_snap_to_the_dropdown_step_of_their_band(self):
        snap = self.rx.snap
        self.assertEqual(snap("-3.80", self.rx.SPH_BANDS), (-3.75, True))
        self.assertEqual(snap("-3.75", self.rx.SPH_BANDS), (-3.75, False))
        # below -6.00 the select steps by 0.50
        self.assertEqual(snap("-7.25", self.rx.SPH_BANDS), (-7.0, True))
        self.assertEqual(snap("-7.50", self.rx.SPH_BANDS), (-7.5, False))
        # outside the range sold: refused, not clamped
        self.assertEqual(snap("-21.00", self.rx.SPH_BANDS), (None, True))
        self.assertEqual(snap("+6.25", self.rx.SPH_BANDS), (None, True))
        self.assertEqual(snap("+2.00", self.rx.CYL_BANDS), (2.0, False))
        self.assertEqual(snap("+7.25", self.rx.CYL_BANDS), (7.0, True))
        self.assertEqual(snap("1.10", self.rx.ADD_BANDS), (1.0, True))
        self.assertEqual(snap("0.50", self.rx.ADD_BANDS), (None, True))

    def test_axis_snaps_to_five_degrees_inside_0_to_180(self):
        self.assertEqual(self.rx.snap_axis("178"), (180, True))
        self.assertEqual(self.rx.snap_axis("92"), (90, True))
        self.assertEqual(self.rx.snap_axis("90"), (90, False))
        self.assertEqual(self.rx.snap_axis("200"), (None, True))

    def test_option_values_match_the_selects_exactly(self):
        ov = self.rx.option_value
        self.assertEqual(ov("sph", -3.75), "-3.75")
        self.assertEqual(ov("sph", 2.0), "2.00")        # the PWR select has no plus sign
        self.assertEqual(ov("cyl", 1.25), "+1.25")      # the CYL select does
        self.assertEqual(ov("cyl", -0.75), "-0.75")
        self.assertEqual(ov("cyl", 0.0), "0.00")
        self.assertEqual(ov("axis", 180), "180")
        self.assertEqual(ov("add", 1.0), "1.00")
        self.assertEqual(ov("sph", None), "")

    def test_a_reading_is_proposed_per_eye_with_notes_for_what_changed(self):
        eyes, conf, notes, unreadable = self.rx.parse_answer(
            '```json\n{"right": {"sph": "-3.80", "cyl": "-0.75", "axis": "178"},'
            ' "left": {"sph": "PL", "cyl": "", "axis": "", "add": "+1.00"},'
            ' "pd": "63", "confidence": 0.91}\n```')
        self.assertFalse(unreadable)
        self.assertEqual(conf, 0.91)
        self.assertEqual(eyes["right"], {"sph": "-3.75", "cyl": "-0.75", "axis": "180", "add": ""})
        self.assertEqual(eyes["left"], {"sph": "0.00", "cyl": "0.00", "axis": "0", "add": "1.00"})
        self.assertTrue(any("Right power rounded" in n for n in notes))
        self.assertTrue(any("Right axis rounded" in n for n in notes))
        self.assertTrue(any("PD read as 63" in n for n in notes))

    def test_an_eye_without_a_power_is_not_proposed(self):
        eyes, _, _, unreadable = self.rx.parse_answer(
            '{"right": {"sph": "-1.00"}, "left": {"cyl": "-0.50", "axis": "90"}}')
        self.assertFalse(unreadable)
        self.assertEqual(eyes["right"]["sph"], "-1.00")
        self.assertIsNone(eyes["left"])

    def test_out_of_range_values_stay_empty_with_a_note(self):
        eyes, _, notes, _ = self.rx.parse_answer(
            '{"right": {"sph": "-22.00", "cyl": "-0.50", "axis": "10"}, "left": {"sph": "-1.00"}}')
        self.assertIsNone(eyes["right"])
        self.assertTrue(any("Right power is outside" in n for n in notes))

    def test_garbage_or_a_refusal_is_unreadable(self):
        for text in ("", "Sorry, I cannot read this.", "[]", '{"unreadable": true, "confidence": 0.2}',
                     '{"right": {}, "left": {}}'):
            eyes, _, _, unreadable = self.rx.parse_answer(text)
            self.assertTrue(unreadable, text)
            self.assertIsNone(eyes)

    def test_the_prompt_is_for_eyeglasses_only(self):
        self.assertIn("EYEGLASSES", self.rx.PROMPT)
        self.assertNotIn("contact lens", self.rx.PROMPT.lower())
        self.assertNotIn('"bc"', self.rx.PROMPT)
        msgs = self.rx.messages_for([b"abc", b"def"])
        self.assertEqual(len(msgs[0]["content"]), 3)
        self.assertTrue(msgs[0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))


class Route(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Blueprint, Flask
        cls.answers = []
        cls.calls = []

        def call_model(**kw):
            cls.calls.append(kw)
            answer = cls.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            resp = types.SimpleNamespace(model="vision-test")
            resp.choices = [types.SimpleNamespace(
                message=types.SimpleNamespace(content=answer))]
            return resp

        cls.rx, cls.ModelError = _load(call_model)
        cls.app = Flask(__name__)
        cls.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        bp = Blueprint("main", __name__)
        cls.rx.register(bp)
        cls.app.register_blueprint(bp)
        cls.client = cls.app.test_client()

    def setUp(self):
        del self.answers[:]
        del self.calls[:]
        _QuotaCursor.counts.clear()
        _QuotaCursor.settings.clear()
        _QuotaCursor.settings[self.rx.ops_controls.RX_TWO_STEP] = 0    # these prove the earlier single read, the rollback
        del _QuotaCursor.flags[:]
        _QuotaCursor.broken[0] = False
        self.rx.TOKEN_MIN_SECONDS = 0
        self.rx.rx_photo_quota.MAX_READS_PER_MINUTE = 10000
        with self.client.session_transaction() as sess:
            sess.clear()

    def _send(self, data=None, name="rx.jpg", human=True, form=None, headers=None, **kw):
        """A request the way the page sends it."""
        token = self.client.get(self.rx.CHALLENGE_ROUTE).get_json()["token"]
        if human:
            with self.client.session_transaction() as sess:
                sess[self.rx.SESSION_HUMAN_KEY] = True
        body = {self.rx.FIELD: (io.BytesIO(data if data is not None else _jpeg()), name), "token": token}
        body.update(form or {})
        return self.client.post(self.rx.ROUTE, data=body, content_type="multipart/form-data",
                                headers=dict({"Origin": "http://localhost"}, **(headers or {})), **kw)

    def _post(self, data=None, name="rx.jpg"):
        return self._send(data, name)

    def test_a_visitor_may_read_a_photo_and_nothing_is_stored(self):
        self.answers.append('{"right": {"sph": "-2.00", "cyl": "-0.50", "axis": "90"},'
                            ' "left": {"sph": "-2.25"}, "confidence": 0.8}')
        r = self._post()
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["eyes"]["right"]["axis"], "90")
        self.assertEqual(body["eyes"]["left"]["cyl"], "0.00")
        self.assertEqual(body["reads_left"], self.rx.MAX_READS_PER_SESSION - 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["workload"], "deepseek_vision")   # no Claude key in this fixture
        self.assertIn("EYEGLASSES", self.calls[0]["messages"][0]["content"][0]["text"])

    def test_the_provider_is_switched_by_configuration(self):
        self.answers.append('{"right": {"sph": "-1.00"}}')
        self.app.config["LENS_RX_VISION_PROVIDER"] = "deepseek_vision"
        try:
            self.assertEqual(self._post().status_code, 200)
        finally:
            self.app.config.pop("LENS_RX_VISION_PROVIDER", None)
        self.assertEqual(self.calls[0]["workload"], "deepseek_vision")

    def test_a_refused_file_never_reaches_the_provider(self):
        r = self._post(b"not an image at all" * 100, name="rx.txt")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["code"], "RX_PHOTO_REJECTED")
        self.assertEqual(self.calls, [])
        token = self.client.get(self.rx.CHALLENGE_ROUTE).get_json()["token"]
        r = self.client.post(self.rx.ROUTE, data={"token": token}, content_type="multipart/form-data",
                             headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 400)

    def test_a_provider_failure_is_a_503_that_says_type_it(self):
        self.answers.append(self.ModelError("down"))
        r = self._post()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["code"], "RX_PHOTO_PROVIDER_FAILED")

    def test_any_provider_error_is_still_an_answer_the_page_can_show(self):
        for failure in (ValueError("bad model"), self.ModelError("down")):
            self.answers.extend([failure, failure])
            r = self._post()
            self.assertEqual(r.status_code, 503, r.data)
            self.assertEqual(r.get_json()["code"], "RX_PHOTO_PROVIDER_FAILED")

    def test_a_crash_outside_the_providers_is_still_json(self):
        keep = self.rx.read_pages
        self.rx.read_pages = lambda pages: 1 / 0
        self.addCleanup(setattr, self.rx, "read_pages", keep)
        r = self._post()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["code"], "RX_PHOTO_PROVIDER_FAILED")

    def test_a_backup_reads_when_the_first_provider_fails(self):
        self.answers.extend([RuntimeError("down"), '{"right": {"sph": "-1.00"}}'])
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual([c["workload"] for c in self.calls], ["deepseek_vision", "openai_vision"])

    def test_unreadable_moves_up_to_the_next_provider(self):
        self.answers.extend(['{"unreadable": true}', '{"right": {"sph": "-1.00"}}'])
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(len(self.calls), 2)

    def test_the_prompt_handles_this_kind_of_paper(self):
        for words in ("sideways", "IS the addition", "marked CL"):
            self.assertIn(words, self.rx.PROMPT)

    def test_an_unreadable_answer_is_a_422(self):
        self.answers.append("I cannot see any prescription here.")
        r = self._post()
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()["code"], "RX_PHOTO_UNREADABLE")

    def test_dropping_the_cookie_does_not_reset_the_limit(self):
        limit = self.rx.ops_controls.SETTINGS[self.rx.ops_controls.RX_PER_IP]["default"]
        reply = '{"right": {"sph": "-1.00"}}'
        for _ in range(limit):
            self.answers.append(reply)
            self.assertEqual(self._post().status_code, 200)
            with self.client.session_transaction() as sess:
                sess.clear()
        r = self._post()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()["code"], "RX_PHOTO_LIMIT")
        self.assertEqual(len(self.calls), limit)        # no paid call past the limit
        self.answers.append(reply)                      # another client is unaffected
        r = self._send(environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
        self.assertEqual(r.status_code, 200)

    def test_a_forged_forwarded_header_is_not_a_new_client(self):
        limit = self.rx.ops_controls.SETTINGS[self.rx.ops_controls.RX_PER_IP]["default"]
        _QuotaCursor.counts[("127.0.0.1", datetime.date.today())] = limit
        r = self._send(headers={"X-Forwarded-For": "198.51.100.7"})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.calls, [])

    def test_changing_the_ip_cannot_pass_the_overall_daily_limit(self):
        quota = self.rx.rx_photo_quota
        _QuotaCursor.counts[(quota.ALL_CLIENTS, datetime.date.today())] = self.rx.ops_controls.SETTINGS[self.rx.ops_controls.RX_PER_DAY]["default"]
        r = self._post()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.calls, [])

    def test_a_refused_file_does_not_use_up_the_limit(self):
        self._post(b"not an image at all" * 100, name="rx.txt")
        self.assertEqual(_QuotaCursor.counts, {})

    def test_no_paid_call_when_the_limit_cannot_be_checked(self):
        _QuotaCursor.broken[0] = True
        r = self._post()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.calls, [])

    def test_ops_can_change_the_limit(self):
        _QuotaCursor.settings[self.rx.ops_controls.RX_PER_IP] = 1
        self.answers.append('{"right": {"sph": "-1.00"}}')
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(self._post().status_code, 429)

    def test_a_burst_is_refused(self):
        self.rx.rx_photo_quota.MAX_READS_PER_MINUTE = 2
        self.answers.extend(['{"right": {"sph": "-1.00"}}'] * 2)
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(self._post().status_code, 429)
        self.assertEqual(len(self.calls), 2)

    def test_bots_are_refused_before_any_paid_call(self):
        for kw in ({"form": {"website_url": "http://spam"}}, {"headers": {"Origin": "http://evil.example"}}):
            r = self._send(**kw)
            self.assertEqual((r.status_code, r.get_json()["code"]), (403, "RX_PHOTO_BOT"), kw)
        r = self.client.post(self.rx.ROUTE, data={self.rx.FIELD: (io.BytesIO(_jpeg()), "rx.jpg"), "token": "forged"},
                             content_type="multipart/form-data", headers={"Origin": "http://localhost"})
        self.assertEqual(r.get_json()["code"], "RX_PHOTO_BOT")
        self.rx.TOKEN_MIN_SECONDS = 60                     # sent faster than a person could
        self.assertEqual(self._post().get_json()["code"], "RX_PHOTO_BOT")
        self.rx.TOKEN_MIN_SECONDS = 0
        token = self.client.get(self.rx.CHALLENGE_ROUTE).get_json()["token"]
        with self.client.session_transaction() as sess:    # a token from another session
            sess.clear()
            sess[self.rx.SESSION_HUMAN_KEY] = True
        r = self.client.post(self.rx.ROUTE, data={self.rx.FIELD: (io.BytesIO(_jpeg()), "rx.jpg"), "token": token},
                             content_type="multipart/form-data", headers={"Origin": "http://localhost"})
        self.assertEqual(r.get_json()["code"], "RX_PHOTO_BOT")
        self.assertEqual(self.calls, [])

    def test_a_guest_solves_the_code_once_and_a_signed_in_customer_does_not(self):
        j = self.client.get(self.rx.CHALLENGE_ROUTE).get_json()
        self.assertTrue(j["captcha"])
        self.assertEqual(j["image"], "IMG")
        self.assertEqual(self._send(human=False).get_json()["code"], "RX_PHOTO_CAPTCHA")
        self.client.get(self.rx.CHALLENGE_ROUTE)
        r = self.client.post(self.rx.VERIFY_ROUTE, json={"captcha": "wrong"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["image"], "IMG")
        self.assertTrue(self.client.post(self.rx.VERIFY_ROUTE, json={"captcha": "ab12"}).get_json()["ok"])
        self.assertFalse(self.client.get(self.rx.CHALLENGE_ROUTE).get_json()["captcha"])
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["user_id"] = 7
        self.assertFalse(self.client.get(self.rx.CHALLENGE_ROUTE).get_json()["captcha"])
        self.answers.append('{"right": {"sph": "-1.00"}}')
        self.assertEqual(self._send(human=False).status_code, 200)

    def test_prohibited_content_is_refused_logged_without_the_image_and_blocks_the_ip(self):
        for category in ("sexual", "child_sexual", "extremist", "illegal", "graphic"):
            _QuotaCursor.counts.clear()
            del _QuotaCursor.flags[:]
            with self.client.session_transaction() as sess:
                sess.clear()
            self.answers.append('{"unreadable": true, "content_flag": "%s"}' % category)
            r = self._post()
            self.assertEqual((r.status_code, r.get_json()["code"]), (422, "RX_PHOTO_BLOCKED"), category)
            self.assertNotIn("eyes", r.get_json())
            row = _QuotaCursor.flags[0]
            self.assertEqual((row[1], len(row[2]), row[3]), (category, 64, "127.0.0.1"))
            self.assertFalse(any(isinstance(v, bytes) for v in row))
        self.calls.clear()
        with self.client.session_transaction() as sess:
            sess.clear()
        r = self._post()                                    # the same IP is now refused before any paid call
        self.assertEqual((r.status_code, r.get_json()["code"]), (403, "RX_PHOTO_BLOCKED"))
        self.assertEqual(self.calls, [])

    def test_an_ordinary_photo_is_not_flagged(self):
        self.assertIsNone(self.rx.content_flag('{"unreadable": true, "content_flag": "none"}'))
        self.assertIsNone(self.rx.content_flag('{"content_flag": "made_up"}'))
        self.assertIsNone(self.rx.content_flag("no json at all"))
        self.answers.append('{"unreadable": true, "content_flag": "none"}')
        self.assertEqual(self._post().get_json()["code"], "RX_PHOTO_UNREADABLE")
        self.assertEqual(_QuotaCursor.flags, [])

    def test_reads_are_capped_per_session(self):
        with self.client.session_transaction() as sess:
            sess[self.rx.SESSION_COUNT_KEY] = self.rx.MAX_READS_PER_SESSION
        r = self._post()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()["code"], "RX_PHOTO_LIMIT")
        self.assertEqual(self.calls, [])


COLOURS = [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (1, 0, 1), (0, 1, 1), (.5, .5, .5), (1, .5, 0)]


def _pdf(pages=5):
    """Built from invented values: a sideways handwriting-style table, a page with a
    contact-lens margin note, a report, a newer typed prescription and its second
    page. Each page has its own colour, so a test can tell which one a reader got."""
    import pymupdf
    doc = pymupdf.open()
    texts = [("R  +1.00  -2.50  x 10", 90), ("CL power RE -2.75/+2.25 x 100", 0), ("Dry eye report", 0),
             ("RE +1.25 -2.75 x 20", 0), ("Advice", 0), ("More", 0), ("More", 0), ("More", 0)]
    for index in range(pages):
        page = doc.new_page(width=595, height=842)
        page.draw_rect(page.rect, color=COLOURS[index], fill=COLOURS[index])
        page.insert_text((200, 400), texts[index][0], fontname="tiit", fontsize=22, rotate=texts[index][1])
    return doc.tobytes()


def _colour(jpeg):
    from PIL import Image
    r, g, b = Image.open(io.BytesIO(jpeg)).convert("RGB").getpixel((5, 5))
    return min(range(len(COLOURS)), key=lambda i: sum(abs(COLOURS[i][k] * 255 - v) for k, v in enumerate((r, g, b))))


def _cell(value="", written=None, doubtful=False):
    return {"value": value, "written": bool(value) if written is None else written, "doubtful": doubtful}


def _eye(sph="", cyl="", axis="", add="", doubtful=()):
    return {"sph": _cell(sph, doubtful="sph" in doubtful), "cyl": _cell(cyl, doubtful="cyl" in doubtful),
            "axis": _cell(axis, doubtful="axis" in doubtful), "add": _cell(add, doubtful="add" in doubtful)}


def _read(right=None, left=None, kind="spectacle_table"):
    return json.dumps({"right": right or _eye("-1.00", "-0.50", "90"), "left": left or _eye("-1.25"),
                       "source": {"kind": kind, "row": "DISTANCE"}})


def _find(*rows, flag="none"):
    return json.dumps({"pages": [{"page": p, "kind": k, "date": d, "rotate": r} for p, k, d, r in rows],
                       "content_flag": flag})


ONE_TABLE = _find((1, "spectacle_table", "2023-09-20", 0))
TWO_TABLES = _find((1, "spectacle_table", "2023-09-20", 90), (2, "visit_notes", "", 0), (3, "report", "", 0),
                   (4, "spectacle_table", "2025-07-02", 0), (5, "other", "", 0))


class FindReadCompare(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flask import Blueprint, Flask
        cls.deepseek = []

        def call_model(**kw):
            answer = cls.deepseek.pop(0)
            if isinstance(answer, Exception):
                raise answer
            resp = types.SimpleNamespace(model="vision-test")
            resp.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=answer))]
            return resp

        cls.rx, cls.ModelError = _load(call_model)
        cls.app = Flask(__name__)
        cls.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        bp = Blueprint("main", __name__)
        cls.rx.register(bp)
        cls.app.register_blueprint(bp)
        cls.client = cls.app.test_client()
        cls.calls = []
        cls.script = {}
        cls.counted = []
        guard = cls.rx.content_guard

        def fake(pages, mime, model, prompt, max_tokens, seconds, effort="low", schema=None, label_pages=False):
            step = "find" if prompt == cls.rx.FIND_PROMPT else "read"
            tier = "haiku" if "haiku" in model else "opus"
            cls.calls.append({"step": step, "tier": tier, "pages": list(pages), "effort": effort, "schema": schema})
            answer = cls.script.get(step + "_" + tier, RuntimeError("not scripted"))
            if isinstance(answer, Exception):
                raise answer
            return answer
        guard._ask_claude = fake
        cls.rx._count = cls.counted.append

    def setUp(self):
        del self.calls[:], self.deepseek[:], self.counted[:]
        self.script.clear()
        self.script.update(find_haiku=ONE_TABLE, read_haiku=_read(), read_opus=_read())
        _QuotaCursor.counts.clear()
        _QuotaCursor.settings.clear()
        del _QuotaCursor.flags[:]
        _QuotaCursor.broken[0] = False
        self.rx.TOKEN_MIN_SECONDS = 0
        self.rx.rx_photo_quota.MAX_READS_PER_MINUTE = 10000
        with self.client.session_transaction() as sess:
            sess.clear()

    def _send(self, data=None, form=None):
        token = self.client.get(self.rx.CHALLENGE_ROUTE).get_json()["token"]
        with self.client.session_transaction() as sess:
            sess[self.rx.SESSION_HUMAN_KEY] = True
        body = {self.rx.FIELD: (io.BytesIO(data if data is not None else _jpeg()), "rx.pdf"), "token": token}
        body.update(form or {})
        return self.client.post(self.rx.ROUTE, data=body, content_type="multipart/form-data",
                                headers={"Origin": "http://localhost"})

    def _reads(self):
        return [c for c in self.calls if c["step"] == "read"]

    def test_the_new_reader_is_on_unless_ops_turn_it_off(self):
        settings = self.rx.ops_controls.SETTINGS[self.rx.ops_controls.RX_TWO_STEP]
        self.assertEqual(settings["default"], 1)
        self.assertTrue(settings["toggle"])

    def test_both_readers_get_only_the_chosen_page_turned_upright(self):
        self.script["find_haiku"] = _find((1, "spectacle_table", "2023-09-20", 90), (2, "contact_lens", "", 0),
                                          (3, "report", "", 0), (4, "other", "", 0), (5, "other", "", 0))
        r = self._send(_pdf())
        self.assertEqual(r.status_code, 200, r.data)
        from PIL import Image
        reads = self._reads()
        self.assertEqual(sorted(c["tier"] for c in reads), ["haiku", "opus"])
        for call in reads:
            self.assertEqual(len(call["pages"]), 1)
            self.assertEqual(_colour(call["pages"][0]), 0)
            width, height = Image.open(io.BytesIO(call["pages"][0])).size
            self.assertGreater(width, height)
            self.assertEqual(call["effort"], "medium")
            self.assertEqual(call["schema"], self.rx.READ_SCHEMA)

    def test_find_sees_every_page_small_and_reads_no_powers(self):
        self.script["find_haiku"] = _find((1, "spectacle_table", "", 0))
        self._send(_pdf())
        find = [c for c in self.calls if c["step"] == "find"][0]
        self.assertEqual(len(find["pages"]), 5)
        from PIL import Image
        self.assertLessEqual(max(Image.open(io.BytesIO(find["pages"][0])).size), self.rx.FIND_SIDE)
        self.assertNotIn("sph", json.dumps(self.rx.FIND_SCHEMA).lower())

    def test_a_contact_lens_page_is_never_read(self):
        self.script["find_haiku"] = _find((1, "visit_notes", "", 0), (2, "contact_lens", "", 0), (3, "report", "", 0))
        r = self._send(_pdf())
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()["code"], "RX_PHOTO_UNREADABLE")
        self.assertEqual(self._reads(), [])

    def test_agreed_values_fill_and_nothing_is_amber(self):
        body = self._send().get_json()
        self.assertEqual(body["eyes"]["right"], {"sph": "-1.00", "cyl": "-0.50", "axis": "90", "add": ""})
        self.assertEqual(body["check"], [])

    def test_a_disagreement_stays_blank_never_cyl_zero(self):
        self.script["read_haiku"] = _read(right=_eye("+3.00", "-1.50", "5"))
        self.script["read_opus"] = _read(right=_eye("+3.00", "-4.50", "5"))
        body = self._send().get_json()
        self.assertEqual(body["eyes"]["right"]["cyl"], "")
        self.assertEqual(body["eyes"]["right"]["sph"], "3.00")
        self.assertEqual(body["check"], [{"eye": "right", "field": "cyl", "label": "Right CYL", "options": ["-1.50", "-4.50"]}])

    def test_a_doubtful_cyl_is_not_no_cylinder(self):
        self.script["read_opus"] = _read(right=_eye("-1.00", "-0.50", "90", doubtful=("cyl",)))
        body = self._send().get_json()
        self.assertEqual(body["eyes"]["right"]["cyl"], "")
        self.assertEqual([c["field"] for c in body["check"]], ["cyl"])

    def test_cyl_not_written_by_either_reader_is_zero(self):
        self.script["read_haiku"] = self.script["read_opus"] = _read(right=_eye("-1.00"))
        body = self._send().get_json()
        self.assertEqual(body["eyes"]["right"], {"sph": "-1.00", "cyl": "0.00", "axis": "0", "add": ""})
        self.assertEqual(body["check"], [])

    def test_one_reader_seeing_cyl_and_the_other_not_is_amber(self):
        self.script["read_haiku"] = _read(right=_eye("-1.00"))
        body = self._send().get_json()
        self.assertEqual(body["eyes"]["right"]["cyl"], "")
        self.assertEqual({c["field"] for c in body["check"]}, {"cyl", "axis"})

    def test_an_eye_with_a_doubtful_sph_is_kept(self):
        self.script["read_haiku"] = _read(right=_eye("-1.00", "-0.50", "90", doubtful=("sph",)))
        body = self._send().get_json()
        self.assertIsNotNone(body["eyes"]["right"])
        self.assertEqual(body["eyes"]["right"]["sph"], "")
        self.assertEqual(body["eyes"]["right"]["cyl"], "-0.50")

    def test_minus_sphere_with_plus_cylinder_is_asked_not_refused(self):
        self.script["read_haiku"] = self.script["read_opus"] = _read(right=_eye("-3.75", "+4.25", "95"))
        body = self._send().get_json()
        self.assertEqual(body["eyes"]["right"]["cyl"], "")
        self.assertEqual(body["eyes"]["right"]["axis"], "")
        self.assertEqual([(c["field"], c["options"]) for c in body["check"]], [("cyl", ["+4.25"]), ("axis", ["95"])])

    def test_two_prescriptions_are_offered_newest_first(self):
        self.script["find_haiku"] = TWO_TABLES
        body = self._send(_pdf()).get_json()
        self.assertTrue(body["choose"])
        self.assertEqual([c["page"] for c in body["choices"]], [4, 1])
        self.assertEqual(body["choices"][0]["label"], "2 July 2025, page 4 (newest)")
        self.assertEqual(body["choices"][1]["label"], "20 September 2023, page 1")
        self.assertEqual([c["selected"] for c in body["choices"]], [True, False])
        self.assertEqual(self._reads(), [])

    def test_choosing_reads_that_page_and_costs_no_second_read(self):
        self.script["find_haiku"] = TWO_TABLES
        pdf = _pdf()
        first = self._send(pdf).get_json()
        chosen = self._send(pdf, form={"choice": first["choices"][1]["token"]}).get_json()
        self.assertEqual(chosen["page"], 1)
        self.assertEqual(chosen["reads_left"], first["reads_left"])
        self.assertEqual(len([c for c in self.calls if c["step"] == "find"]), 1)
        from PIL import Image
        for call in self._reads():
            self.assertEqual(_colour(call["pages"][0]), 0)
            width, height = Image.open(io.BytesIO(call["pages"][0])).size
            self.assertGreater(width, height)       # its own turn came with the choice

    def test_a_choice_cannot_be_used_on_another_file_or_session(self):
        self.script["find_haiku"] = TWO_TABLES
        pdf = _pdf()
        token = self._send(pdf).get_json()["choices"][0]["token"]
        r = self._send(_jpeg(), form={"choice": token})
        self.assertEqual(r.get_json()["code"], "RX_PHOTO_CHOICE_EXPIRED")
        with self.client.session_transaction() as sess:
            sess.clear()
        r = self._send(pdf, form={"choice": token})
        self.assertEqual(r.get_json()["code"], "RX_PHOTO_CHOICE_EXPIRED")
        self.assertEqual(self._reads(), [])

    def test_a_table_only_one_reader_vouches_for_is_all_amber(self):
        self.script["read_opus"] = _read(kind="other")
        body = self._send().get_json()
        self.assertEqual({c["field"] for c in body["check"] if c["eye"] == "right"}, {"sph", "cyl", "axis"})

    def test_a_page_neither_reader_calls_a_table_is_refused(self):
        self.script["read_haiku"] = self.script["read_opus"] = _read(kind="other")
        self.assertEqual(self._send().status_code, 422)

    def test_the_same_file_gives_the_same_answer_and_no_new_read(self):
        data = _jpeg()
        first = self._send(data).get_json()
        calls = len(self.calls)
        self.script["read_haiku"] = _read(right=_eye("-9.00"))
        second = self._send(data).get_json()
        self.assertEqual(second["eyes"], first["eyes"])
        self.assertEqual(second["reads_left"], first["reads_left"])
        self.assertEqual(len(self.calls), calls)

    def test_deepseek_alone_old_shape_is_converted_and_every_field_amber(self):
        del self.script["read_haiku"]
        del self.script["read_opus"]
        self.deepseek.append('{"right": {"sph": "-2.00", "cyl": "-0.50", "axis": "90"}, "left": {"sph": "-2.25"}}')
        body = self._send().get_json()
        self.assertEqual(body["eyes"]["right"], {"sph": "-2.00", "cyl": "-0.50", "axis": "90", "add": ""})
        self.assertEqual([c["field"] for c in body["check"] if c["eye"] == "right"], ["sph", "cyl", "axis"])
        self.assertEqual([c["options"] for c in body["check"] if c["eye"] == "left"][1], ["0.00"])
        self.assertEqual(self.counted[-1], "DeepSeek")

    def test_deepseek_alone_in_the_new_shape_is_every_field_amber(self):
        del self.script["read_haiku"]
        del self.script["read_opus"]
        self.deepseek.append(_read())
        body = self._send().get_json()
        self.assertEqual(len([c for c in body["check"] if c["eye"] == "right"]), 3)

    def test_nobody_answering_asks_the_customer_to_try_again(self):
        del self.script["read_haiku"]
        del self.script["read_opus"]
        self.deepseek.append(self.ModelError("down"))
        self.deepseek.append(self.ModelError("down"))
        r = self._send()
        self.assertEqual(r.status_code, 503)
        self.assertIn("try again", r.get_json()["message"])

    def test_ops_tile_counts_find_and_each_read(self):
        self._send()
        self.assertEqual(self.counted, ["Claude Haiku", "Claude Haiku", "Claude Opus"])

    def test_ops_tile_shows_a_claude_reader_that_did_not_answer(self):
        del self.script["read_opus"]
        self._send()
        self.assertEqual(self.counted, ["Claude Haiku", "Claude Haiku", self.rx.content_guard.NOT_CHECKED])

    def test_haiku_flag_in_find_opus_clean_goes_on_to_read(self):
        self.script["find_haiku"] = _find(flag="sexual")
        self.script["find_opus"] = ONE_TABLE
        r = self._send()
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(self._reads()), 2)
        self.assertEqual(_QuotaCursor.flags, [])

    def test_haiku_flag_in_find_opus_agrees_is_blocked_and_flagged(self):
        self.script["find_haiku"] = self.script["find_opus"] = _find(flag="sexual")
        r = self._send()
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()["code"], "RX_PHOTO_BLOCKED")
        self.assertEqual(len(_QuotaCursor.flags), 1)
        self.assertEqual(self._reads(), [])

    def test_haiku_flag_in_find_opus_silent_is_unchecked_nothing_held_against_them(self):
        self.script["find_haiku"] = _find(flag="sexual")
        r = self._send()
        self.assertEqual(r.status_code, 503)
        self.assertIn("try again", r.get_json()["message"])
        self.assertEqual(_QuotaCursor.flags, [])
        self.assertEqual(self._reads(), [])
        self.assertEqual(self.counted, [self.rx.content_guard.NOT_CHECKED])

    def test_a_backup_flag_keeps_todays_policy(self):
        del self.script["find_haiku"]
        self.deepseek.append(_find(flag="graphic"))
        r = self._send()
        self.assertEqual(r.status_code, 422)
        self.assertEqual(len(_QuotaCursor.flags), 1)

    def test_more_than_six_pages_says_so(self):
        self.script["find_haiku"] = _find((1, "spectacle_table", "", 0))
        body = self._send(_pdf(8)).get_json()
        self.assertTrue(any("first 6 pages" in n for n in body["notes"]))
        self.assertEqual(len([c for c in self.calls if c["step"] == "find"][0]["pages"]), 6)

    def test_the_routes_that_store_files_still_get_two_pages(self):
        pdf = _pdf()
        self.assertEqual(len(self.rx.lens_documents.normalise(pdf)["pages"]), 2)
        self.assertEqual(len(self.rx.lens_documents.normalise(pdf, pdf_pages=6)["pages"]), 5)

    def test_the_session_limit_still_applies_to_new_files(self):
        with self.client.session_transaction() as sess:
            sess[self.rx.SESSION_COUNT_KEY] = self.rx.MAX_READS_PER_SESSION
        self.assertEqual(self._send().status_code, 429)
        self.assertEqual(self.calls, [])

    def test_a_choice_cannot_be_spent_without_end(self):
        self.script["find_haiku"] = TWO_TABLES
        pdf = _pdf()
        token = self._send(pdf).get_json()["choices"][0]["token"]
        self.script["read_haiku"] = self.script["read_opus"] = "not json"
        self.deepseek.extend([self.ModelError("down")] * 40)
        codes = [self._send(pdf, form={"choice": token}).status_code for _ in range(self.rx.READER_PAGES + 1)]
        self.assertEqual(codes[-1], 429)

    def test_the_page_blocks_continuing_until_amber_is_settled(self):
        page = open(os.path.join(REPO, "templates", "prescription.html"), encoding="utf-8").read()
        self.assertIn("if (window.rxChecksOpen) {", page)
        self.assertIn("if (window.rxChecksOpen) { e.preventDefault();", page)
        self.assertIn("Which one should we use?", page)
        self.assertIn("Read this one", page)
        self.assertIn("css/rx-page.css') }}?v=8", page)


class Wiring(unittest.TestCase):

    def test_the_page_offers_the_scan_and_the_module_is_registered_and_deployed(self):
        page = open(os.path.join(REPO, "templates", "prescription.html"), encoding="utf-8").read()
        self.assertIn('id="rxPhotoInput"', page)
        self.assertIn("/api/rx-photo/read", page)
        # the selects the reader fills are the ones the cart route reads
        for name in ("right_pwr", "right_cyl", "right_axis", "right_add",
                     "left_pwr", "left_cyl", "left_axis", "left_add"):
            self.assertIn('id="%s"' % name, page)
        models = open(os.path.join(REPO, "models.py"), encoding="utf-8").read()
        self.assertIn("rx_photo.register(bp)", models)
        deploy = open(os.path.join(REPO, "deploy", "deploy.py"), encoding="utf-8").read()
        self.assertIn('"rx_photo.py"', deploy)
        self.assertIn('"static/css/rx-page.css"', deploy)
        self.assertIn('"templates/prescription.html"', deploy)
        # new files must be declared new, or the deploy tool blocks on their absence
        new_block = deploy[deploy.index("NEW_IN_RELEASE = ("):]
        new_block = new_block[:new_block.index('")\n') + 2]
        for name in ('"rx_photo.py"', '"static/css/rx-page.css"', '"static/css/ow-ui.css"'):
            self.assertIn(name, new_block)


if __name__ == "__main__":
    unittest.main()
