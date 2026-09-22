"""Optiwar AI and the customer's faces (FACE-D).

The assistant reads what My Faces knows — who exists, who is scanned, how the
frame on the page fits each of them, who each cart frame is for — and may
change three things (who they shop for, the default person, the person on a
cart line) only through the same offer -> PENDING -> yes -> EXECUTED/FAILED
ledger every other action uses. A stranger's profile, a contact-lens line, a
stale line or a malformed tag is refused before any row changes, and the
refusal is recorded.
"""
import copy
import importlib.util
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_face_profiles import (  # noqa: E402
    AVAILABLE, C1, C2, LEGACY_DDL, _connect, _load_service, _wipe,
)
from test_face_fit import MEAS, PRODUCTS_DDL  # noqa: E402
from test_face_cart import _frame_line, _lens_line, PERSISTENT_CART_DDL  # noqa: E402

PKG = "fa_pkg"
_ORDER = ["FACE_ACTION_OFFERED", "ACTION_CONFIRMED", "ACTION_EXECUTED",
          "ACTION_FAILED", "ACTION_BLOCKED"]
SID = "face-assistant-test-session"


def _load_pkg(fp_mod):
    """face_assistant.py with its siblings, all bound to one face_profiles."""
    for name in [n for n in sys.modules if n.startswith(PKG + ".")]:
        del sys.modules[name]
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [REPO]
    sys.modules[PKG] = pkg
    sys.modules[PKG + ".face_profiles"] = fp_mod
    mods = {}
    for name in ("acr", "lens_cart", "face_fit", "face_cart", "face_assistant"):
        spec = importlib.util.spec_from_file_location(
            PKG + "." + name, os.path.join(REPO, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        mods[name] = mod
    return mods


class _Session(dict):
    modified = False


class PureRuleTests(unittest.TestCase):
    """Gate, tags and decline words: no database."""

    @classmethod
    def setUpClass(cls):
        cls.fa = _load_pkg(_load_service())["face_assistant"]

    def test_gate_needs_the_profile_gate_and_its_own_flag(self):
        fa = self.fa
        base = {"FACE_PROFILES_ENABLED": "1",
                "FACE_PROFILES_ALLOW_EMAILS": "a@x.com"}
        self.assertFalse(fa.enabled_for("a@x.com", dict(base)))
        env = dict(base, FACE_ASSISTANT_ENABLED="1")
        self.assertTrue(fa.enabled_for("a@x.com", env))
        self.assertFalse(fa.enabled_for("b@x.com", env))
        env = dict(env, FACE_ASSISTANT_ALLOW_EMAILS="b@x.com")
        self.assertFalse(fa.enabled_for("b@x.com", env),
                         "the profile gate still excludes b")
        self.assertFalse(fa.enabled_for("a@x.com", env),
                         "the assistant list no longer names a")
        self.assertFalse(fa.enabled_for("a@x.com", {"FACE_ASSISTANT_ENABLED": "1"}))
        self.assertFalse(fa.enabled_for(None, env))

    def test_tags_are_stripped_and_the_first_one_counts(self):
        fa = self.fa
        reply, offer = fa.extract("Shall I? [ACTION:FACE_SHOP_FOR:12] ok "
                                  "[ACTION:FACE_DEFAULT:3]")
        self.assertEqual(reply, "Shall I?  ok")
        self.assertEqual(offer, (fa.SHOP_FOR, "12"))
        reply, offer = fa.extract("Line [ACTION:FACE_CART_LINE: 1 : 0 ]")
        self.assertEqual(offer, (fa.CART_LINE, "1 : 0"))
        self.assertEqual(fa.extract("plain"), ("plain", None))
        self.assertEqual(fa.extract("[ACTION:NAVIGATE:/cart]"),
                         ("[ACTION:NAVIGATE:/cart]", None))

    def test_decline_words(self):
        fa = self.fa
        for t in ("no", "No.", "not now", "cancel", "never mind", "don't"):
            self.assertTrue(fa.is_decline(t), t)
        for t in ("yes", "no idea what to do", "", None, "note this"):
            self.assertFalse(fa.is_decline(t), t)

    def test_a_short_offer_window(self):
        self.assertLessEqual(self.fa.PENDING_TTL_SECONDS, 600)


@unittest.skipUnless(AVAILABLE, "MariaDB test database not reachable")
class FaceAssistantTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fp = _load_service()
        cls.db = _connect()
        cur = cls.db.cursor()
        cur.execute(LEGACY_DDL)
        for t in ("contact_lens_variants", "contact_lens_param_rules",
                  "contact_lens_images", "contact_lens_products", "products"):
            cur.execute("DROP TABLE IF EXISTS " + t)
        cur.execute(PRODUCTS_DDL)
        cur.execute("INSERT INTO products (product_id, product_code, product_name, "
                    "product_size, product_category) VALUES "
                    "(9800101, 'FIT1', 'Fits', '52-18-140', 'Spectacles Frame'),"
                    "(9800102, 'FIT2', 'Wide', '58-22-160', 'Spectacles Frame'),"
                    "(9800103, 'LENS1', 'A lens', '', 'Contact Lenses')")
        cur.execute(PERSISTENT_CART_DDL)
        cls.db.commit()
        cls.fp.ensure_schema(cls.db)
        mods = _load_pkg(cls.fp)
        cls.fa = mods["face_assistant"]
        cls.fc = mods["face_cart"]
        cls.ff = mods["face_fit"]
        cls.acr = mods["acr"]
        cls.fc.ensure_schema(cls.db)
        cls.acr.ensure_schema(lambda: _connect())

    @classmethod
    def tearDownClass(cls):
        cls._clean()
        cur = cls.db.cursor()
        cur.execute("DROP TABLE IF EXISTS products")
        cls.db.commit()
        cls.db.close()

    @classmethod
    def _clean(cls):
        _wipe(cls.db, C1, C2)
        cur = cls.db.cursor()
        cur.execute("DELETE FROM ai_events WHERE session_id=%s", (SID,))
        cur.execute("DELETE FROM ai_actions WHERE session_id=%s", (SID,))
        cls.db.commit()

    def setUp(self):
        self._clean()
        self.me = self.fp.ensure_self(self.db, C1, "Sudhanshu")
        self.mother = self.fp.create_profile(self.db, C1, "Mother", "parent", consent=True)
        self.fp.record_scan(self.db, C1, self.mother["id"], MEAS)
        self.stranger = self.fp.ensure_self(self.db, C2, "Other")
        self.fp.record_scan(self.db, C2, self.stranger["id"], MEAS)
        self.cart = [_frame_line(9800101, "FIT1"), _lens_line(),
                     _frame_line(9800102, "FIT2", price=2999)]
        self.session = _Session()

    def _actions(self):
        cur = self.db.cursor()
        cur.execute("SELECT action_type, target, status, result_code FROM ai_actions "
                    "WHERE session_id=%s ORDER BY created_at", (SID,))
        return cur.fetchall()

    def _events(self):
        cur = self.db.cursor()
        cur.execute("SELECT event_type, action_type, failure_code, success, journey_stage "
                    "FROM ai_events WHERE session_id=%s", (SID,))
        return sorted(cur.fetchall(), key=lambda e: _ORDER.index(e["event_type"]))

    def _offer(self, action_type, target, cart=None):
        checked = self.fa.describe(self.db, C1, self.cart if cart is None else cart,
                                   action_type, target)
        self.fa.offer(self.db, SID, checked)
        return checked

    def _say_yes(self, cart=None):
        """What the gateway does on a bare yes: confirm, execute, record."""
        action_type, pending = self.fa.live_pending(self.db, SID)
        self.assertIsNotNone(pending)
        self.acr.mark_action(self.db, pending["action_id"], "CONFIRMED")
        try:
            reply, changed = self.fa.execute(
                self.db, C1, self.session, self.cart if cart is None else cart,
                action_type, pending["target"])
        except self.fa.FaceActionError as e:
            self.fa.record_outcome(self.db, SID, pending["action_id"], action_type,
                                   False, code=e.code)
            return None, e
        self.fa.record_outcome(self.db, SID, pending["action_id"], action_type, True)
        return reply, changed

    # -- reading ------------------------------------------------------------

    def test_read_model_lists_people_scans_and_cart_frames(self):
        product = self.fa.page_frame(self.db, "https://optiwar.in/product?pid=9800101")
        self.assertEqual(product["product_code"], "FIT1")
        m = self.fa.read_model(self.db, C1, self.session, self.cart, product)
        by_name = {p["display_name"]: p for p in m["people"]}
        me, mother = by_name["Sudhanshu"], by_name["Mother"]
        self.assertTrue(me["is_self"] and me["is_default"] and me["is_active"])
        self.assertFalse(me["has_scan"])
        self.assertIsNone(me["measurements"])
        self.assertTrue(mother["has_scan"])
        self.assertEqual(mother["measurements"]["pd_far"], 63.0)
        self.assertEqual(mother["measurements"]["face_width"], 132.0)
        self.assertEqual(mother["page_fit"]["label"], self.ff.evaluate(MEAS, "52-18-140")["label"])
        self.assertEqual(me["page_fit"]["label"], self.ff.evaluate({}, "52-18-140")["label"])
        self.assertEqual([ln["index"] for ln in m["frame_lines"]], [0, 2],
                         "the contact-lens line is not a frame line")
        self.assertEqual(m["frame_lines"][0]["person"], "No person / Gift")
        self.assertEqual(m["active_profile_id"], me["id"])
        self.assertFalse(m["explicit_nobody"])
        self.assertNotIn("Other", by_name, "another customer's person is not read")

    def test_prompt_section_says_the_fit_and_the_rules_and_no_stranger(self):
        product = self.fa.page_frame(self.db, "https://optiwar.in/product?pid=9800101")
        m = self.fa.read_model(self.db, C1, self.session, self.cart, product)
        text = self.fa.prompt_section(m)
        self.assertIn("Mother (Parent) — scanned: PD 63.0 mm, face width 132.0 mm", text)
        self.assertIn("not scanned yet", text)
        self.assertIn("Self, default, SHOPPING FOR NOW", text)
        self.assertIn("fit of the frame on this page:", text)
        self.assertIn("line 0: Frame FIT1 (FIT1) -> No person / Gift", text)
        self.assertIn("[ACTION:FACE_CART_LINE:<line index>:<id>]", text)
        self.assertIn("never recompute or guess a fit", text)
        self.assertNotIn("Other", text)

    def test_a_lens_page_or_no_page_gives_no_page_fit(self):
        self.assertIsNone(self.fa.page_frame(self.db, "https://optiwar.com/product?pid=9800103"))
        self.assertIsNone(self.fa.page_frame(self.db, "https://optiwar.in/cart"))
        m = self.fa.read_model(self.db, C1, self.session, [], None)
        self.assertIsNone(m["page_frame"])
        self.assertTrue(all(p["page_fit"] is None for p in m["people"]))
        self.assertEqual(m["frame_lines"], [])

    def test_nobody_selected_reads_as_no_person(self):
        self.ff.set_active(self.db, C1, self.session, 0)
        m = self.fa.read_model(self.db, C1, self.session, [], None)
        self.assertIsNone(m["active_profile_id"])
        self.assertTrue(m["explicit_nobody"])
        self.assertIn("Shopping for now: No person / Gift", self.fa.prompt_section(m))

    # -- the three changes, through the ledger ---------------------------------

    def test_shop_for_is_offered_confirmed_executed(self):
        checked = self._offer(self.fa.SHOP_FOR, str(self.mother["id"]))
        self.assertEqual(checked["summary"], "shop for Mother from now on")
        self.assertEqual([a["status"] for a in self._actions()], ["PENDING"])
        self.assertEqual(self.session, {}, "an offer changes nothing")
        reply, changed = self._say_yes()
        self.assertIn("shopping for Mother", reply)
        self.assertFalse(changed)
        self.assertEqual(self.session[self.ff.SESSION_KEY], self.mother["id"])
        self.assertEqual([a["status"] for a in self._actions()], ["EXECUTED"])
        events = self._events()
        self.assertEqual([e["event_type"] for e in events],
                         ["FACE_ACTION_OFFERED", "ACTION_CONFIRMED", "ACTION_EXECUTED"])
        self.assertEqual({e["action_type"] for e in events}, {"FACE_SHOP_FOR"})
        self.assertEqual(events[0]["journey_stage"], "SUPPORT",
                         "a face offer is not a navigation offer")

    def test_shop_for_nobody(self):
        self._offer(self.fa.SHOP_FOR, "0")
        reply, _ = self._say_yes()
        self.assertIn("No person / Gift", reply)
        self.assertEqual(self.session[self.ff.SESSION_KEY], 0)
        self.assertIsNone(self.ff.active_profile(self.db, C1, self.session))

    def test_default_person_changes_only_after_yes(self):
        self._offer(self.fa.SET_DEFAULT, str(self.mother["id"]))
        self.assertTrue(self.fp.get_profile(self.db, C1, self.me["id"])["is_default"])
        reply, _ = self._say_yes()
        self.assertIn("Mother is now the default", reply)
        self.assertTrue(self.fp.get_profile(self.db, C1, self.mother["id"])["is_default"])
        self.assertFalse(self.fp.get_profile(self.db, C1, self.me["id"])["is_default"])
        self.assertEqual([a["status"] for a in self._actions()], ["EXECUTED"])

    def test_default_cannot_be_nobody(self):
        with self.assertRaises(self.fa.FaceActionError) as cm:
            self.fa.describe(self.db, C1, self.cart, self.fa.SET_DEFAULT, "0")
        self.assertEqual(cm.exception.code, "bad_target")

    def test_cart_line_assignment_and_unassignment(self):
        checked = self._offer(self.fa.CART_LINE, "2:%d" % self.mother["id"])
        self.assertEqual(checked["target"], "2:%d:9800102" % self.mother["id"])
        self.assertIn("Frame FIT2", checked["summary"])
        before = copy.deepcopy(self.cart)
        reply, changed = self._say_yes()
        self.assertTrue(changed)
        self.assertIn("now for Mother", reply)
        self.assertIn("fit:", reply)
        self.assertEqual(self.cart[2][self.fc.LINE_KEY], self.mother["id"])
        self.assertNotIn(self.fc.LINE_KEY, self.cart[0])
        for i in (0, 1):
            self.assertEqual(self.cart[i], before[i])
        self._offer(self.fa.CART_LINE, "2:0")
        reply, changed = self._say_yes()
        self.assertIn("No person / Gift", reply)
        self.assertEqual(self.cart[2][self.fc.LINE_KEY], 0)
        self.assertEqual([a["status"] for a in self._actions()], ["EXECUTED", "EXECUTED"])

    # -- refusals -----------------------------------------------------------

    def test_a_strangers_person_is_refused_for_all_three(self):
        sid = str(self.stranger["id"])
        for action_type, target in ((self.fa.SHOP_FOR, sid), (self.fa.SET_DEFAULT, sid),
                                    (self.fa.CART_LINE, "0:" + sid)):
            with self.assertRaises(self.fa.FaceActionError) as cm:
                self.fa.describe(self.db, C1, self.cart, action_type, target)
            self.assertEqual(cm.exception.code, "not_your_person", action_type)
        # and at execution, should a proposal somehow carry it
        for action_type, target in ((self.fa.SHOP_FOR, sid), (self.fa.SET_DEFAULT, sid),
                                    (self.fa.CART_LINE, "0:%s:9800101" % sid)):
            with self.assertRaises(self.fa.FaceActionError):
                self.fa.execute(self.db, C1, self.session, self.cart, action_type, target)
        self.assertNotIn(self.fc.LINE_KEY, self.cart[0])
        self.assertTrue(self.fp.get_profile(self.db, C2, self.stranger["id"])["is_default"])
        self.assertEqual(self.session, {})

    def test_malformed_targets(self):
        fa = self.fa
        cases = [(fa.SHOP_FOR, "abc"), (fa.SHOP_FOR, "-1"), (fa.SHOP_FOR, ""),
                 (fa.SET_DEFAULT, "x"), (fa.SET_DEFAULT, "-3"),
                 (fa.CART_LINE, "0"), (fa.CART_LINE, "a:1"), (fa.CART_LINE, "0:b"),
                 (fa.CART_LINE, "-1:0"), (fa.CART_LINE, "0:-1"), (fa.CART_LINE, "0:1:2"),
                 ("FACE_NOTHING", "1")]
        for action_type, target in cases:
            with self.assertRaises(fa.FaceActionError, msg=(action_type, target)):
                fa.describe(self.db, C1, self.cart, action_type, target)
        with self.assertRaises(fa.FaceActionError):
            fa.execute(self.db, C1, self.session, self.cart, fa.CART_LINE, "0:1")

    def test_lens_line_other_line_and_missing_line_are_refused(self):
        fa = self.fa
        with self.assertRaises(fa.FaceActionError) as cm:
            fa.describe(self.db, C1, self.cart, fa.CART_LINE, "1:%d" % self.mother["id"])
        self.assertEqual(cm.exception.code, "not_a_frame")
        cart = [dict(_frame_line(9800101, "FIT1"), product_category="Hearing Aids")]
        with self.assertRaises(fa.FaceActionError) as cm:
            fa.describe(self.db, C1, cart, fa.CART_LINE, "0:0")
        self.assertEqual(cm.exception.code, "not_a_frame")
        with self.assertRaises(fa.FaceActionError) as cm:
            fa.describe(self.db, C1, self.cart, fa.CART_LINE, "7:0")
        self.assertEqual(cm.exception.code, "no_such_line")

    def test_a_cart_that_moved_fails_the_confirmed_action(self):
        self._offer(self.fa.CART_LINE, "0:%d" % self.mother["id"])
        del self.cart[0]  # the frame at line 0 is now FIT2's lens neighbour
        reply, err = self._say_yes()
        self.assertIsNone(reply)
        self.assertEqual(err.code, "line_mismatch")
        self.assertNotIn(self.fc.LINE_KEY, self.cart[1])
        self.assertEqual([(a["status"], a["result_code"]) for a in self._actions()],
                         [("FAILED", "line_mismatch")])
        self.assertEqual([e["event_type"] for e in self._events()],
                         ["FACE_ACTION_OFFERED", "ACTION_CONFIRMED", "ACTION_FAILED"])

    def test_a_person_deleted_between_offer_and_yes_fails(self):
        self._offer(self.fa.SET_DEFAULT, str(self.mother["id"]))
        self.fp.delete_profile(self.db, C1, self.mother["id"])
        reply, err = self._say_yes()
        self.assertIsNone(reply)
        self.assertEqual(err.code, "not_your_person")
        self.assertTrue(self.fp.get_profile(self.db, C1, self.me["id"])["is_default"])
        self.assertEqual([a["status"] for a in self._actions()], ["FAILED"])

    # -- the ledger's shape --------------------------------------------------

    def test_a_second_yes_finds_nothing_to_do(self):
        self._offer(self.fa.SHOP_FOR, str(self.mother["id"]))
        self._say_yes()
        self.assertEqual(self.fa.live_pending(self.db, SID), (None, None))
        self.assertEqual([a["status"] for a in self._actions()], ["EXECUTED"])

    def test_a_newer_offer_supersedes_the_older_and_only_it_runs(self):
        self._offer(self.fa.SHOP_FOR, str(self.mother["id"]))
        self._offer(self.fa.SHOP_FOR, "0")
        action_type, pending = self.fa.live_pending(self.db, SID)
        self.assertEqual(pending["target"], "0")
        self._say_yes()
        self.assertEqual(self.session[self.ff.SESSION_KEY], 0)
        self.assertEqual(sorted(a["status"] for a in self._actions()),
                         ["EXECUTED", "SUPERSEDED"])

    def test_an_expired_offer_is_not_executed(self):
        self._offer(self.fa.SHOP_FOR, str(self.mother["id"]))
        cur = self.db.cursor()
        cur.execute("UPDATE ai_actions SET expires_at=NOW() - INTERVAL 1 MINUTE "
                    "WHERE session_id=%s", (SID,))
        self.db.commit()
        self.assertEqual(self.fa.live_pending(self.db, SID), (None, None))
        self.assertEqual(self.session, {})

    def test_declined_and_blocked_are_recorded(self):
        self._offer(self.fa.SHOP_FOR, str(self.mother["id"]))
        _, pending = self.fa.live_pending(self.db, SID)
        self.acr.mark_action(self.db, pending["action_id"], self.fa.ST_DECLINED)
        self.assertEqual([a["status"] for a in self._actions()], ["DECLINED"])
        self.assertEqual(self.fa.live_pending(self.db, SID), (None, None))
        self.fa.record_blocked(self.db, SID, self.fa.CART_LINE, "not_a_frame",
                               page_url="/cart")
        ev = self._events()[-1]
        self.assertEqual((ev["event_type"], ev["action_type"], ev["failure_code"]),
                         ("ACTION_BLOCKED", "FACE_CART_LINE", "not_a_frame"))


class GatewayWiringTests(unittest.TestCase):
    """The chat gateway reaches the module in the right places."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "chat_gateway.py"), encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_the_customer_is_the_browsers_login(self):
        body = self.src[self.src.index("def _face_context("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("flask_session.get('user_id')", body)
        self.assertIn("face_assistant.enabled_for(", body)
        self.assertNotIn("data.get('customer_id')", body)

    def test_the_section_is_in_the_prompt_and_the_tag_is_handled_after_cleaning(self):
        src = self.src
        self.assertIn("face_ctx = _face_context(db, session, page_url)", src)
        self.assertIn("faces_section) if s))", src)
        clean = src.index("ai_reply, actions, navigate_url = _clean_ai_reply(ai_reply)")
        self.assertLess(clean, src.index("ai_reply = _face_offer(db, session_id, face_ctx, ai_reply, page_url)"))
        self.assertIn("ai_reply, face_result = _face_confirmation(db, session_id, face_ctx,", src)
        self.assertIn("_confirm_is_navigational = (face_result is None and", src)
        self.assertIn("resp['face_action'] = face_result", src)

    def test_a_yes_to_a_face_offer_is_settled_before_the_model_is_asked(self):
        src = self.src
        conf = src.index("ai_reply, face_result = _face_confirmation(")
        self.assertLess(src.index("ai_reply = _confirmed_ask_reply(history, content, contact_name)"), conf)
        self.assertLess(conf, src.index("if ai_reply is None:\n"))

    def test_the_widget_reloads_after_an_executed_face_action(self):
        with open(os.path.join(REPO, "static", "js", "chat-widget.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("data.face_action && data.face_action.ok && data.face_action.reload", js)
        with open(os.path.join(REPO, "templates", "base.html"), encoding="utf-8") as fh:
            self.assertIn("chat-widget.js') + '?v=23'", fh.read())

    def test_deployable(self):
        with open(os.path.join(REPO, "deploy", "deploy.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().count('"face_assistant.py"'), 2)



@unittest.skipUnless(AVAILABLE, "MariaDB test database not reachable")
class GatewayFunctionalTests(unittest.TestCase):
    """The real chat_gateway helpers, in a Flask request, against the database:
    an ungated account gets no section and its tag is blocked; a gated one is
    offered, and a yes executes through the ledger."""

    @classmethod
    def setUpClass(cls):
        from flask import Flask
        import test_ai_wrapper  # loads flaskr.chat_gateway with its stubs
        cls.cg = test_ai_wrapper.cg
        cls.fa = sys.modules["flaskr.face_assistant"]
        cls.fp = sys.modules["flaskr.face_profiles"]
        cls.acr = sys.modules["flaskr.acr"]
        cls.db = _connect()
        cur = cls.db.cursor()
        cur.execute(LEGACY_DDL)
        cur.execute("DROP TABLE IF EXISTS products")
        cur.execute(PRODUCTS_DDL)
        cur.execute("INSERT INTO products (product_id, product_code, product_name, "
                    "product_size, product_category) VALUES "
                    "(9800101, 'FIT1', 'Fits', '52-18-140', 'Spectacles Frame')")
        cls.db.commit()
        cls.fp.ensure_schema(cls.db)
        cls.acr.ensure_schema(lambda: _connect())
        cls.app = Flask(__name__)
        cls.app.config.update(TESTING=True, SECRET_KEY="t")

    @classmethod
    def tearDownClass(cls):
        cls._clean()
        cur = cls.db.cursor()
        cur.execute("DROP TABLE IF EXISTS products")
        cls.db.commit()
        cls.db.close()

    @classmethod
    def _clean(cls):
        _wipe(cls.db, C1, C2)
        cur = cls.db.cursor()
        cur.execute("DELETE FROM ai_events WHERE session_id=%s", (SID,))
        cur.execute("DELETE FROM ai_actions WHERE session_id=%s", (SID,))
        cls.db.commit()

    def setUp(self):
        self._clean()
        self.me = self.fp.ensure_self(self.db, C1, "Sudhanshu")
        self.mother = self.fp.create_profile(self.db, C1, "Mother", "parent", consent=True)
        self.env = {"FACE_PROFILES_ENABLED": "1", "FACE_ASSISTANT_ENABLED": "1",
                    "FACE_PROFILES_ALLOW_EMAILS": "lensebazaar@gmail.com"}
        self.chat = {"session_id": SID, "contact_email": "lensebazaar@gmail.com"}

    def _owner_cookie(self, session_id=SID):
        with self.app.app_context():
            return self.cg._chat_cookie_serializer().dumps(session_id)

    def _ctx(self, user_id=C1, owner=True):
        headers = {"Cookie": "ow_chat_token=%s" % self._owner_cookie()} if owner else {}
        ctx = self.app.test_request_context("/api/chat/message", headers=headers)
        ctx.push()
        from flask import session
        if user_id:
            session["user_id"] = user_id
        return ctx

    def _events(self):
        cur = self.db.cursor()
        cur.execute("SELECT event_type, failure_code FROM ai_events WHERE session_id=%s", (SID,))
        return cur.fetchall()

    def test_no_login_or_no_gate_means_no_section_and_a_blocked_tag(self):
        from unittest import mock
        with mock.patch.dict(os.environ, self.env, clear=False):
            ctx = self._ctx(user_id=None)
            try:
                self.assertIsNone(self.cg._face_context(self.db, self.chat, "/cart"))
            finally:
                ctx.pop()
            ctx = self._ctx()
            try:
                self.assertIsNone(self.cg._face_context(
                    self.db, {"contact_email": "someone@else.com"}, "/cart"))
                reply = self.cg._face_offer(
                    self.db, SID, None, "Sure. [ACTION:FACE_SHOP_FOR:%d]" % self.mother["id"], "/cart")
            finally:
                ctx.pop()
        self.assertEqual(reply, "Sure.")
        self.assertEqual([(e["event_type"], e["failure_code"]) for e in self._events()],
                         [("ACTION_BLOCKED", "not_enabled")])
        self.assertEqual(self.fa.live_pending(self.db, SID), (None, None))

    def test_a_browser_that_does_not_own_the_chat_gets_no_face_context(self):
        """A signed-in customer naming somebody else's chat session (no signed
        owner cookie for it) can neither read faces nor settle that session's
        pending face action."""
        from unittest import mock
        with mock.patch.dict(os.environ, self.env, clear=False):
            ctx = self._ctx()
            try:
                face_ctx = self.cg._face_context(self.db, self.chat, "/cart")
                self.cg._face_offer(self.db, SID, face_ctx,
                                    "Ok? [ACTION:FACE_DEFAULT:%d]" % self.mother["id"], "/cart")
            finally:
                ctx.pop()
            for headers in ({}, {"Cookie": "ow_chat_token=%s" % self._owner_cookie("chat_other")}):
                ctx = self.app.test_request_context("/api/chat/message", headers=headers)
                ctx.push()
                try:
                    from flask import session
                    session["user_id"] = C1
                    self.assertIsNone(self.cg._face_context(self.db, self.chat, "/cart"))
                finally:
                    ctx.pop()
        cur = self.db.cursor()
        cur.execute("SELECT status FROM ai_actions WHERE session_id=%s", (SID,))
        self.assertEqual([r["status"] for r in cur.fetchall()], ["PENDING"])
        self.assertFalse(self.fp.get_profile(self.db, C1, self.mother["id"])["is_default"])

    def test_a_yes_answers_the_question_asked_last_whatever_its_type(self):
        from unittest import mock
        from flask import session
        with mock.patch.dict(os.environ, self.env, clear=False):
            ctx = self._ctx()
            try:
                face_ctx = self.cg._face_context(self.db, self.chat, "/cart")
                self.cg._face_offer(self.db, SID, face_ctx,
                                    "A? [ACTION:FACE_SHOP_FOR:%d]" % self.mother["id"], "/cart")
                self.cg._face_offer(self.db, SID, face_ctx,
                                    "B? [ACTION:FACE_DEFAULT:%d]" % self.mother["id"], "/cart")
                action_type, _ = self.fa.live_pending(self.db, SID)
                self.assertEqual(action_type, "FACE_DEFAULT")
                reply, result = self.cg._face_confirmation(self.db, SID, face_ctx, "yes", "/cart")
                self.assertTrue(result["ok"])
                self.assertEqual(result["type"], "FACE_DEFAULT")
                self.assertNotIn("face_shop_pid", session, "the older question was not answered")
            finally:
                ctx.pop()
        self.assertTrue(self.fp.get_profile(self.db, C1, self.mother["id"])["is_default"])
        cur = self.db.cursor()
        cur.execute("SELECT action_type, status FROM ai_actions WHERE session_id=%s "
                    "ORDER BY action_type", (SID,))
        self.assertEqual([(r["action_type"], r["status"]) for r in cur.fetchall()],
                         [("FACE_DEFAULT", "EXECUTED"), ("FACE_SHOP_FOR", "SUPERSEDED")])

    def test_a_second_confirmation_of_the_same_row_does_not_execute_again(self):
        """Two requests read the same pending row; only the one whose UPDATE
        claimed it executes and records the outcome."""
        from unittest import mock
        with mock.patch.dict(os.environ, self.env, clear=False):
            ctx = self._ctx()
            try:
                face_ctx = self.cg._face_context(self.db, self.chat, "/cart")
                self.cg._face_offer(self.db, SID, face_ctx,
                                    "Ok? [ACTION:FACE_DEFAULT:%d]" % self.mother["id"], "/cart")
                _, pending = self.fa.live_pending(self.db, SID)
                with mock.patch.object(self.fa, "live_pending",
                                       return_value=("FACE_DEFAULT", pending)):
                    r1 = self.cg._face_confirmation(self.db, SID, face_ctx, "yes", "/cart")
                    r2 = self.cg._face_confirmation(self.db, SID, face_ctx, "yes", "/cart")
            finally:
                ctx.pop()
        self.assertTrue(r1[1]["ok"])
        self.assertFalse(r2[1]["ok"])
        self.assertEqual(r2[1]["code"], "already_settled")
        self.assertIn("already been settled", r2[0])
        types = [e["event_type"] for e in self._events()]
        self.assertEqual(types.count("ACTION_CONFIRMED"), 1)
        self.assertEqual(types.count("ACTION_EXECUTED"), 1)

    def test_an_offer_that_was_not_stored_is_not_asked(self):
        from unittest import mock
        with mock.patch.dict(os.environ, self.env, clear=False):
            ctx = self._ctx()
            try:
                face_ctx = self.cg._face_context(self.db, self.chat, "/cart")
                with mock.patch.object(self.fa, "offer", return_value=None), \
                        mock.patch.object(self.cg.dev_defects, "record") as rec:
                    reply = self.cg._face_offer(
                        self.db, SID, face_ctx,
                        "I can do that. [ACTION:FACE_DEFAULT:%d]" % self.mother["id"], "/cart")
            finally:
                ctx.pop()
        self.assertNotIn("(yes/no)", reply)
        self.assertIn("can't make that change right now", reply)
        self.assertEqual(rec.call_args[0][0], "CHAT_FACE_OFFER_NOT_STORED")
        self.assertEqual(self.fa.live_pending(self.db, SID), (None, None))

    def test_gated_account_is_offered_then_a_yes_executes(self):
        from unittest import mock
        from flask import session
        with mock.patch.dict(os.environ, self.env, clear=False):
            ctx = self._ctx()
            try:
                face_ctx = self.cg._face_context(
                    self.db, self.chat, "https://optiwar.in/product?pid=9800101")
                self.assertEqual(face_ctx["customer_id"], C1)
                self.assertIn("Mother (Parent)", face_ctx["section"])
                self.assertIn("Frame on this page: Fits", face_ctx["section"])
                # not a yes/no turn: nothing to settle
                self.assertEqual(self.cg._face_confirmation(
                    self.db, SID, face_ctx, "which frames fit mother?", "/x"), (None, None))
                reply = self.cg._face_offer(
                    self.db, SID, face_ctx,
                    "I can switch to Mother. [ACTION:FACE_SHOP_FOR:%d]" % self.mother["id"], "/x")
                self.assertEqual(reply, "I can switch to Mother.\n\nShall I shop for Mother from now on? (yes/no)")
                self.assertNotIn("face_shop_pid", session, "an offer changes nothing")
                reply, result = self.cg._face_confirmation(self.db, SID, face_ctx, "yes", "/x")
                self.assertTrue(result["ok"] and result["reload"])
                self.assertIn("shopping for Mother", reply)
                self.assertEqual(session["face_shop_pid"], self.mother["id"])
                # the same yes again: nothing pending, the model answers
                self.assertEqual(self.cg._face_confirmation(self.db, SID, face_ctx, "yes", "/x"),
                                 (None, None))
            finally:
                ctx.pop()
        self.assertEqual(sorted(e["event_type"] for e in self._events()),
                         ["ACTION_CONFIRMED", "ACTION_EXECUTED", "FACE_ACTION_OFFERED"])
        cur = self.db.cursor()
        cur.execute("SELECT DISTINCT journey_stage FROM ai_events WHERE session_id=%s", (SID,))
        self.assertEqual([r["journey_stage"] for r in cur.fetchall()], ["SUPPORT"],
                         "the whole face lifecycle is a support fact, never navigation")

    def test_a_no_declines_and_a_strangers_id_is_blocked_with_a_reason(self):
        from unittest import mock
        stranger = self.fp.ensure_self(self.db, C2, "Other")
        with mock.patch.dict(os.environ, self.env, clear=False):
            ctx = self._ctx()
            try:
                face_ctx = self.cg._face_context(self.db, self.chat, "/cart")
                reply = self.cg._face_offer(
                    self.db, SID, face_ctx, "Ok. [ACTION:FACE_DEFAULT:%d]" % stranger["id"], "/cart")
                self.assertIn("I don't know that person", reply)
                self.assertEqual(self.fa.live_pending(self.db, SID), (None, None))
                self.cg._face_offer(self.db, SID, face_ctx,
                                    "Ok? [ACTION:FACE_DEFAULT:%d]" % self.mother["id"], "/cart")
                reply, result = self.cg._face_confirmation(self.db, SID, face_ctx, "no", "/cart")
                self.assertEqual(reply, "Okay — nothing changed.")
                self.assertFalse(result["ok"])
            finally:
                ctx.pop()
        self.assertTrue(self.fp.get_profile(self.db, C1, self.me["id"])["is_default"])
        self.assertTrue(self.fp.get_profile(self.db, C2, stranger["id"])["is_default"])
        cur = self.db.cursor()
        cur.execute("SELECT status FROM ai_actions WHERE session_id=%s", (SID,))
        self.assertEqual([r["status"] for r in cur.fetchall()], ["DECLINED"])
        codes = [e["failure_code"] for e in self._events() if e["event_type"] == "ACTION_BLOCKED"]
        self.assertEqual(codes, ["not_your_person"])

if __name__ == "__main__":
    unittest.main()
