"""Optiwar AI and the customer's faces: what it may read, and the three
things it may change.

Reading: for an account the Face Profiles gate admits, the assistant's system
prompt carries the customer's people — who they are, who has a scan and what
it measured, who the customer is shopping for right now, who each spectacle
frame in the cart is for, and how the frame on the current page fits each
person. Everything comes from ``face_profiles``, ``face_fit`` and
``face_cart``; the assistant computes no fit of its own.

Changing: three actions, each a tag the model emits while it asks the
customer a yes/no question::

    [ACTION:FACE_SHOP_FOR:<profile id | 0>]        who the customer shops for
    [ACTION:FACE_DEFAULT:<profile id>]             the account's default person
    [ACTION:FACE_CART_LINE:<line index>:<profile id | 0>]   who a frame line is for

A tag never executes. The server strips it, checks the target against the
customer's own people and cart, and records a PENDING action in the same
``ai_actions`` ledger the navigation actions use. The next turn, a bare
"yes" confirms exactly that action and the server executes it itself —
through ``face_fit.set_active``, ``face_profiles.set_default`` and
``face_cart.assign``, the same code the buttons call — and records EXECUTED
or FAILED; a "no" records DECLINED. A stranger's profile id, a contact-lens
line or a missing line is refused before anything is recorded as pending, and
recorded as BLOCKED.

The customer whose people are read and changed is the one the browser is
signed in as (the Flask session), never the id the widget sent when the chat
started.
"""
import os
import re

from . import acr
from . import face_cart
from . import face_fit
from . import face_profiles as fp

ENABLED_ENV = "FACE_ASSISTANT_ENABLED"
ALLOW_ENV = "FACE_ASSISTANT_ALLOW_EMAILS"

SHOP_FOR = "FACE_SHOP_FOR"
SET_DEFAULT = "FACE_DEFAULT"
CART_LINE = "FACE_CART_LINE"
ACTION_TYPES = (SHOP_FOR, SET_DEFAULT, CART_LINE)

# A yes to "shall I set Mother as who you shop for?" must arrive soon after
# the question; an old offer must not be executed by an unrelated "yes".
PENDING_TTL_SECONDS = 300

ST_DECLINED = "DECLINED"
ST_BLOCKED = "BLOCKED"

TAG_RE = re.compile(r"\[ACTION:(FACE_SHOP_FOR|FACE_DEFAULT|FACE_CART_LINE):([^\]]*)\]")

_DECLINE_RE = re.compile(
    r"^\s*(no|nope|nah|not now|never ?mind|cancel|stop|leave it|don'?t|do not)"
    r"\b[\s!.,]*$", re.IGNORECASE)

_PID_RE = re.compile(r"[?&]pid=(\d+)")

FRAME_CATEGORY = "Spectacles Frame"


class FaceActionError(Exception):
    """An action the assistant proposed that cannot be done as stated."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------

def enabled_for(email, environ=None):
    """Its own flag and allow-list (falling back to the Face Profiles list),
    and never on for an account the profile gate excludes."""
    env = os.environ if environ is None else environ
    if not fp.enabled_for(email, env.get("FACE_PROFILES_ENABLED"),
                          env.get("FACE_PROFILES_ALLOW_EMAILS")):
        return False
    allow = env.get(ALLOW_ENV) or env.get("FACE_PROFILES_ALLOW_EMAILS")
    return fp.enabled_for(email, env.get(ENABLED_ENV), allow)


def is_decline(text):
    return bool(_DECLINE_RE.match(text or ""))


# --------------------------------------------------------------------------
# read model
# --------------------------------------------------------------------------

def page_frame(db, page_url):
    """The spectacle frame whose page the customer is on, or None."""
    m = _PID_RE.search(page_url or "")
    if not m:
        return None
    cur = db.cursor()
    cur.execute("SELECT product_id, product_name, product_code, product_size, "
                "product_category FROM products WHERE product_id=%s",
                (int(m.group(1)),))
    row = cur.fetchone()
    if not row or (row.get("product_category") or "") != FRAME_CATEGORY:
        return None
    return row


def _person(row, active_id, product):
    ref = face_fit._profile_ref(row)
    meas = face_fit.profile_measurement(row)
    ref["is_active"] = active_id is not None and int(row["id"]) == active_id
    ref["measurements"] = None
    ref["page_fit"] = None
    if ref["has_scan"]:
        rec = face_fit.recommended_dimensions(meas)
        ref["measurements"] = {
            "pd_far": face_fit._num(meas.get("pd_far")),
            "face_width": face_fit._num(meas.get("face_width")),
            "recommended_size": rec["size"] if rec else None,
        }
    if product is not None:
        fit = face_fit.evaluate(meas, product.get("product_size"))
        ref["page_fit"] = {"label": fit["label"], "reasons": fit["reasons"]}
    return ref


def read_model(db, customer_id, session, cart, product=None):
    """Everything the assistant may know about this customer's faces."""
    rows = fp.list_profiles(db, customer_id)
    active = face_fit.active_profile(db, customer_id, session)
    active_id = int(active["id"]) if active else None
    people = [_person(r, active_id, product) for r in rows]
    names = {p["id"]: p["display_name"] for p in people}
    lines = []
    for index, item in enumerate(cart or []):
        if not face_cart.is_frame_line(item):
            continue
        pid = face_cart._stored(item)
        lines.append({
            "index": index,
            "product_id": item.get("product_id"),
            "product_name": item.get("product_name"),
            "product_code": item.get("product_code"),
            "profile_id": pid,
            "person": names.get(pid, face_cart.NO_PERSON_LABEL),
        })
    return {
        "people": people,
        "active_profile_id": active_id,
        "explicit_nobody": session.get(face_fit.SESSION_KEY) == face_fit.NOBODY,
        "frame_lines": lines,
        "page_frame": ({"product_id": product["product_id"],
                        "product_name": product.get("product_name"),
                        "product_size": product.get("product_size")}
                       if product is not None else None),
    }


def prompt_section(model):
    """The system-prompt section built from ``read_model``."""
    out = ["CUSTOMER'S FACES (My Faces; authoritative, from the face engine):"]
    if not model["people"]:
        out.append("  This customer has no face profiles yet. They can scan at "
                   "/tryon or add people under My Faces (/profile/?tab=faces).")
    for p in model["people"]:
        flags = []
        if p["is_self"]:
            flags.append("Self")
        if p["is_default"]:
            flags.append("default")
        if p["is_active"]:
            flags.append("SHOPPING FOR NOW")
        m = p["measurements"]
        if m:
            scan = "scanned: PD %s mm, face width %s mm, recommended size %s" % (
                m["pd_far"], m["face_width"], m["recommended_size"] or "unknown")
        else:
            scan = "not scanned yet"
        line = "  - id %d: %s (%s%s) — %s" % (
            p["id"], p["display_name"], p["relationship_label"],
            "; " + ", ".join(flags) if flags else "", scan)
        if p["page_fit"]:
            line += " — fit of the frame on this page: %s" % p["page_fit"]["label"]
            if p["page_fit"]["reasons"]:
                line += " (%s)" % "; ".join(p["page_fit"]["reasons"])
        out.append(line)
    if model["people"] and model["active_profile_id"] is None:
        out.append("  Shopping for now: No person / Gift (no fit is checked).")
    pf = model["page_frame"]
    if pf:
        out.append("  Frame on this page: %s (id %s, size %s)." % (
            pf["product_name"], pf["product_id"], pf["product_size"] or "not stated"))
    if model["frame_lines"]:
        out.append("  Spectacle frames in the cart (line index: frame -> for whom):")
        for ln in model["frame_lines"]:
            out.append("    line %d: %s (%s) -> %s" % (
                ln["index"], ln["product_name"], ln["product_code"], ln["person"]))
    out.append(
        "  Fit verdicts above are final: report them, never recompute or guess a fit.\n"
        "  Only spectacle frames are fitted to a face; a contact lens never is.\n"
        "  You may change three things, each ONLY by asking a yes/no question and\n"
        "  putting the matching tag in the same reply (the change happens only after\n"
        "  the customer answers yes; never say it is done):\n"
        "    - who they shop for: [ACTION:FACE_SHOP_FOR:<id>] (0 = No person / Gift)\n"
        "    - their default person: [ACTION:FACE_DEFAULT:<id>]\n"
        "    - who a cart frame line is for: [ACTION:FACE_CART_LINE:<line index>:<id>] (0 = nobody)\n"
        "  Use only ids and line indexes listed above. Never invent a person; if the\n"
        "  customer names someone not listed, say they can add them under My Faces.")
    return "\n".join(out)


# --------------------------------------------------------------------------
# the model's tag -> a checked proposal
# --------------------------------------------------------------------------

def extract(reply):
    """Strip every face tag; return ``(reply, (action_type, target) | None)``.
    The first tag counts; a reply may carry only one change."""
    found = TAG_RE.findall(reply or "")
    if not found:
        return reply, None
    reply = TAG_RE.sub("", reply or "").strip()
    action_type, target = found[0]
    return reply, (action_type, target.strip())


def _int(value, code, message):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise FaceActionError(code, message)


def _name(db, customer_id, pid):
    if pid == face_fit.NOBODY:
        return face_cart.NO_PERSON_LABEL
    try:
        row = fp.get_profile(db, customer_id, pid)
    except fp.ProfileError:
        raise FaceActionError("not_your_person", "I don't know that person on your account.")
    return row["display_name"]


def describe(db, customer_id, cart, action_type, target):
    """Check a proposed action against the customer's own people and cart and
    say what it would do. Raises ``FaceActionError`` for anything the
    customer could not do with the buttons either."""
    if action_type == SHOP_FOR:
        pid = _int(target, "bad_target", "Which person?")
        if pid < 0:
            raise FaceActionError("bad_target", "Which person?")
        name = _name(db, customer_id, pid)
        return {"type": action_type, "target": str(pid),
                "summary": "shop for %s from now on" % name}
    if action_type == SET_DEFAULT:
        pid = _int(target, "bad_target", "Which person?")
        if pid <= 0:
            raise FaceActionError("bad_target", "The default must be a person.")
        name = _name(db, customer_id, pid)
        return {"type": action_type, "target": str(pid),
                "summary": "make %s the default person on your account" % name}
    if action_type == CART_LINE:
        parts = [p.strip() for p in str(target).split(":")]
        if len(parts) != 2:
            raise FaceActionError("bad_target", "Which cart line, for whom?")
        index = _int(parts[0], "bad_line", "Which cart line?")
        pid = _int(parts[1], "bad_target", "Which person?")
        if index < 0 or pid < 0:
            raise FaceActionError("bad_line", "Which cart line?")
        try:
            item = cart[index]
        except (IndexError, TypeError):
            raise FaceActionError("no_such_line", "That cart line no longer exists.")
        if not face_cart.is_frame_line(item):
            raise FaceActionError("not_a_frame", "Only a spectacle frame is fitted to a face.")
        name = _name(db, customer_id, pid)
        return {"type": action_type,
                "target": "%d:%d:%s" % (index, pid, item.get("product_id")),
                "summary": "set the %s in your cart to be for %s" % (
                    item.get("product_name") or "frame", name)}
    raise FaceActionError("bad_action", "I can't do that.")


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def execute(db, customer_id, session, cart, action_type, target):
    """Do one checked action for the signed-in customer. Returns
    ``(message, cart_changed)``; raises ``FaceActionError`` when the world
    moved since the offer (cart changed, person deleted)."""
    if action_type == SHOP_FOR:
        pid = _int(target, "bad_target", "Which person?")
        try:
            row = face_fit.set_active(db, customer_id, session, pid)
        except fp.ProfileError:
            raise FaceActionError("not_your_person", "That person is no longer on your account.")
        session.modified = True
        if row is None:
            return ("Done — you're now shopping for no one in particular "
                    "(No person / Gift), so no face fit is checked."), False
        return "Done — you're now shopping for %s." % row["display_name"], False
    if action_type == SET_DEFAULT:
        pid = _int(target, "bad_target", "Which person?")
        try:
            row = fp.set_default(db, customer_id, pid)
        except fp.ProfileError:
            raise FaceActionError("not_your_person", "That person is no longer on your account.")
        return "Done — %s is now the default person on your account." % row["display_name"], False
    if action_type == CART_LINE:
        parts = str(target).split(":")
        if len(parts) != 3:
            raise FaceActionError("bad_target", "Which cart line, for whom?")
        index, pid, product_id = parts
        try:
            item = face_cart.assign(db, customer_id, cart, index, product_id, pid)
        except face_cart.LineError as e:
            raise FaceActionError(e.code, str(e))
        except fp.ProfileError:
            raise FaceActionError("not_your_person", "That person is no longer on your account.")
        name = _name(db, customer_id, item[face_cart.LINE_KEY])
        fit = face_cart.line_fit(db, customer_id, item)
        return ("Done — the %s in your cart is now for %s (fit: %s)." % (
            item.get("product_name") or "frame", name, fit["label"])), True
    raise FaceActionError("bad_action", "I can't do that.")


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------

def offer(db, session_id, checked):
    """Record a checked proposal as the session's live PENDING face action."""
    return acr.create_pending_action(
        db, session_id, checked["type"], checked["target"],
        ttl_seconds=PENDING_TTL_SECONDS, offer_event=acr.EV_FACE_ACTION_OFFERED,
        journey_stage=acr.STAGE_SUPPORT)


def live_pending(db, session_id):
    """The latest live pending face action of any of the three types."""
    for action_type in ACTION_TYPES:
        row = acr.get_live_pending_action(db, session_id, action_type)
        if row and row.get("target"):
            return action_type, row
    return None, None


def record_blocked(db, session_id, action_type, code, page_url=None):
    acr.log_event(db, acr.EV_ACTION_BLOCKED, session_id=session_id,
                  journey_stage=acr.STAGE_SUPPORT, action_type=action_type,
                  page_url=page_url, success=False, failure_code=code)


def record_outcome(db, session_id, action_id, action_type, success, code=None,
                   page_url=None):
    acr.mark_action(db, action_id, "EXECUTED" if success else "FAILED",
                    result_code=code)
    acr.log_event(db, acr.EV_ACTION_EXECUTED if success else acr.EV_ACTION_FAILED,
                  session_id=session_id, action_id=action_id,
                  action_type=action_type, journey_stage=acr.STAGE_SUPPORT,
                  page_url=page_url, success=bool(success), failure_code=code)
