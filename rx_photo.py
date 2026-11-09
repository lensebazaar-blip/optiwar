"""An eyeglasses prescription photographed on the frames lens page, read by the
vision model and proposed into the page's own dropdowns.

Eyeglasses only: this is the spectacle-lens page (``templates/prescription.html``,
``main.add_prescription``), which takes a prescription through eight
``<select>``s (SPH, CYL, AXIS per eye, ADD optional). This module adds a second
door onto the same selects: the customer photographs the paper, the configured
vision workload (``LENS_RX_VISION_PROVIDER``: ``openai_vision`` live,
``deepseek_vision`` ready — the same setting the other photo readers use)
transcribes it with a spectacle-only prompt, and the values are *proposed*
into the dropdowns. The customer checks and confirms;
``main.add_to_cart_with_lenses`` receives the selects exactly as if typed.

What is different from the contact-lens upload (``lens_upload.py``):

* **Nothing is stored.** The photo is normalised in memory (orientation,
  size, re-encoded by ``lens_documents.normalise``), shown to the model, and
  dropped. A copy the customer wants kept with the order is the checkout
  page's own upload, which has its retention and Ops trail.
* **No sign-in.** A visitor may read a photo. Reads are counted per session
  and capped, and counted again per IP and overall in the database
  (``rx_photo_quota``), so an open endpoint cannot spend the model's budget.
* **Values are snapped to the dropdowns, never invented.** Each value the
  model transcribed is rounded to the step the select offers (0.25, or 0.50
  in the high ranges; axis to 5 degrees) and refused when it lies outside
  the range sold. A refused field stays empty for the customer to fill.
"""
import datetime
import json
import os
import re
import secrets
import time

from urllib.parse import urlsplit

from flask import current_app, jsonify, request, session

try:
    from . import ai_client, content_guard, dev_defects, lens_documents, ops_controls, rx_photo_quota
except ImportError:  # run as a plain module (tests)
    import ai_client
    import dev_defects
    import lens_documents
    import content_guard
    import ops_controls
    import rx_photo_quota

FIELD = "rx_photo"
ROUTE = "/api/rx-photo/read"
CHALLENGE_ROUTE = "/api/rx-photo/challenge"
VERIFY_ROUTE = "/api/rx-photo/verify"
SESSION_COUNT_KEY = "rx_photo_reads"
SESSION_NONCE_KEY = "rx_photo_nonce"
SESSION_HUMAN_KEY = "rx_photo_human"
SESSION_CAPTCHA_KEY = "rx_photo_captcha"
HONEYPOT = "website_url"
TOKEN_SALT = "rx-photo-challenge"
TOKEN_MIN_SECONDS = 1.5             # no person picks a file faster than this
TOKEN_MAX_SECONDS = 2 * 60 * 60
# What the model may report instead of "none". Each one refuses the upload.
BLOCKED_CATEGORIES = content_guard.CATEGORIES
MAX_READS_PER_SESSION = 8
EYES = ("right", "left")

# The ranges of the selects in models.add_prescription, as (lo, hi, step)
# bands. A value outside every band is refused; a value inside is snapped to
# the band's step.
SPH_BANDS = ((-20.0, -6.0, 0.5), (-6.0, 6.0, 0.25))
CYL_BANDS = ((-8.5, -6.0, 0.5), (-6.0, 6.0, 0.25), (6.0, 8.0, 0.5))
ADD_BANDS = ((1.0, 3.0, 0.25),)
AXIS_RANGE = (0, 180)
AXIS_STEP = 5

# One question for every provider, in the same words; one answer shape. The
# model transcribes; it never picks a value the paper does not carry.
PROMPT = (
    "You are reading a photograph or scan of an EYEGLASSES (spectacle) "
    "prescription for the customer who owns it. Transcribe only what is "
    "written; do not guess or infer missing values.\n"
    "Return ONLY a JSON object, no prose, of the form:\n"
    '{"right": {"sph": "-3.75", "cyl": "-0.75", "axis": "180", "add": ""}, '
    '"left": {"sph": "", "cyl": "", "axis": "", "add": ""}, "pd": "", '
    '"confidence": 0.0-1.0, "unreadable": false, "notes": "short"}\n'
    "Rules: 'right' is OD / R / RE / Right Eye; 'left' is OS / L / LE / Left "
    "Eye. sph is SPH, SPHERE, PWR, POWER or DS. cyl is CYL or CYLINDER. axis is "
    "AXIS (1 to 180). add is ADD, ADDITION, NEAR ADD or N.V. (a positive "
    "reading power); if the paper gives separate DISTANCE and NEAR rows, give "
    "the distance row as sph/cyl/axis and the difference as add. Use signed "
    "decimals with two places (\"-0.50\", \"+1.25\"); 'plano', 'PL', 'DS' "
    "or a dash is \"0.00\". Leave a field \"\" when it is not written. Omit "
    "an eye entirely if nothing is written for it. pd is the pupillary "
    "distance in mm if written. Set unreadable=true when the document is not "
    "an eyeglasses prescription or cannot be read. Never include the "
    "patient's name, age, phone or any other personal detail.\n"
    "Also add \"content_flag\" to the JSON, judged from the whole image: "
    "\"child_sexual\" for any sexualised depiction of a minor; \"sexual\" for "
    "nudity or sexual content; \"extremist\" for terrorist or violent "
    "extremist material; \"illegal\" for other clearly unlawful material; "
    "\"graphic\" for graphic violence or gore; otherwise \"none\". An ordinary "
    "photo or document is \"none\" even when it is not a prescription."
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


# --------------------------------------------------------------------------
# values
# --------------------------------------------------------------------------

def _number(value):
    """A float from what the model wrote (``"-3.75"``, ``"+1,25"``, ``"pl"``), or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        return None
    if text in ("pl", "plano", "0", "0.0", "0.00", "-", "—", "sph", "ds", "dsph"):
        return 0.0
    match = _NUMBER.search(text.replace(" ", ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def snap(value, bands):
    """``(snapped, changed)``: the value on the nearest step of the band it
    lies in, or ``(None, True)`` when no band holds it."""
    number = _number(value)
    if number is None:
        return None, False
    for lo, hi, step in bands:
        if lo - 1e-9 <= number <= hi + 1e-9:
            snapped = round(round(number / step) * step, 2)
            snapped = min(hi, max(lo, snapped))
            return snapped, abs(snapped - number) > 1e-9
    return None, True


def snap_axis(value):
    number = _number(value)
    if number is None:
        return None, False
    lo, hi = AXIS_RANGE
    if number < lo or number > hi:
        return None, True
    snapped = int(round(number / AXIS_STEP) * AXIS_STEP)
    snapped = min(hi, max(lo, snapped))
    return snapped, abs(snapped - number) > 1e-9


def option_value(field, number):
    """The ``<option value>`` the page's select carries for a snapped value."""
    if number is None:
        return ""
    if field == "axis":
        return str(int(number))
    if field == "cyl" and number > 0:
        return "+%.2f" % number
    return "%.2f" % number


def propose(raw):
    """``(eyes, notes)`` from the model's parsed JSON: per eye the select
    values to propose (``""`` where nothing usable was read) and a list of
    short notes about values that were snapped or refused."""
    eyes = {}
    notes = []
    for eye in EYES:
        values = raw.get(eye) if isinstance(raw, dict) else None
        if not isinstance(values, dict):
            eyes[eye] = None
            continue
        out = {"sph": "", "cyl": "", "axis": "", "add": ""}
        label = "Right" if eye == "right" else "Left"
        sph, changed = snap(values.get("sph", values.get("pwr")), SPH_BANDS)
        if sph is None and _number(values.get("sph", values.get("pwr"))) is not None:
            notes.append("%s power is outside the range sold here; please choose it yourself." % label)
        elif changed:
            notes.append("%s power rounded to the nearest step." % label)
        out["sph"] = option_value("sph", sph)
        cyl, changed = snap(values.get("cyl"), CYL_BANDS)
        if cyl is None and _number(values.get("cyl")) is None:
            cyl, changed = 0.0, False   # nothing written for CYL means none
        if cyl is None and _number(values.get("cyl")) is not None:
            notes.append("%s cylinder is outside the range sold here; please choose it yourself." % label)
        elif changed:
            notes.append("%s cylinder rounded to the nearest step." % label)
        if cyl is not None and abs(cyl) < 1e-9:
            cyl = 0.0
        out["cyl"] = option_value("cyl", cyl)
        axis, changed = snap_axis(values.get("axis"))
        if axis is None and _number(values.get("axis")) is not None:
            notes.append("%s axis must be 0 to 180; please choose it yourself." % label)
        elif changed:
            notes.append("%s axis rounded to the nearest 5 degrees." % label)
        if cyl is None or abs(cyl) < 1e-9:
            axis = 0 if axis is None else axis
        out["axis"] = option_value("axis", axis)
        add, changed = snap(values.get("add"), ADD_BANDS)
        add_number = _number(values.get("add"))
        if add_number is not None and add_number > 0 and add is None:
            notes.append("%s ADD is outside +1.00 to +3.00; please choose it yourself." % label)
        elif changed and add is not None:
            notes.append("%s ADD rounded to the nearest step." % label)
        out["add"] = option_value("add", add) if add else ""
        eyes[eye] = out if out["sph"] else None
    return eyes, notes


class ContentBlocked(Exception):
    def __init__(self, category):
        super().__init__(category)
        self.category = category


def content_flag(text):
    """The refused category the model reported, or None."""
    match = re.search(r'"content_flag"\s*:\s*"([a-z_]+)"', text or "", re.IGNORECASE)
    category = match.group(1).lower() if match else ""
    return category if category in BLOCKED_CATEGORIES else None


def parse_answer(text):
    """``(eyes, confidence, notes, unreadable)`` from the model's reply."""
    cleaned = _FENCE.sub("", (text or "").strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None, None, [], True
    try:
        raw = json.loads(cleaned[start:end + 1])
    except ValueError:
        return None, None, [], True
    if not isinstance(raw, dict):
        return None, None, [], True
    confidence = raw.get("confidence")
    try:
        confidence = round(min(1.0, max(0.0, float(confidence))), 2)
    except (TypeError, ValueError):
        confidence = None
    if raw.get("unreadable") is True:
        return None, confidence, [], True
    eyes, notes = propose(raw)
    pd = str(raw.get("pd") or "").strip()[:12]
    if pd:
        notes.append("PD read as %s mm (not needed for this order)." % pd)
    if not any(eyes.values()):
        return None, confidence, notes, True
    return eyes, confidence, notes, False


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------

def workload():
    return lens_documents.provider_name(
        current_app.config.get("LENS_RX_VISION_PROVIDER")
        or os.environ.get("LENS_RX_VISION_PROVIDER"))


def messages_for(pages_jpeg):
    """The chat-completion messages: the spectacle prompt + inline pages."""
    import base64  # noqa: PLC0415
    content = [{"type": "text", "text": PROMPT}]
    for page in pages_jpeg:
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," +
                   base64.b64encode(page).decode("ascii"),
            "detail": "high"}})
    return [{"role": "user", "content": content}]


def read_pages(pages, endpoint=ROUTE):
    """Ask the configured provider; ``(eyes, confidence, notes, model, unreadable)``.

    Provider failures propagate as ``ai_client.ModelError``.
    """
    name = workload()
    resp = ai_client.call_model(
        workload=name, messages=messages_for(pages),
        max_tokens=400, temperature=0, endpoint=endpoint)
    text = ""
    try:
        text = resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        pass
    category = content_flag(text)
    if category:
        raise ContentBlocked(category)
    eyes, confidence, notes, unreadable = parse_answer(text)
    return eyes, confidence, notes, getattr(resp, "model", None) or name, unreadable


def _refuse(code, message, status=400):
    dev_defects.record(code, where="rx_photo")
    return jsonify({"ok": False, "code": code, "message": message}), status


def reads_left():
    return max(0, MAX_READS_PER_SESSION - int(session.get(SESSION_COUNT_KEY) or 0))


def _client_ip():
    """The address ProxyFix took from our own proxy. Not the first X-Forwarded-For
    value: a caller can write that one, and this limit is what stops the caller."""
    return (request.remote_addr or "")[:45]


def _serializer():
    from itsdangerous import URLSafeSerializer  # noqa: PLC0415
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt=TOKEN_SALT)


def _new_token():
    nonce = session.get(SESSION_NONCE_KEY) or secrets.token_hex(8)
    session[SESSION_NONCE_KEY] = nonce
    session.modified = True
    return _serializer().dumps({"n": nonce, "t": time.time()})


def _token_ok(token):
    """Issued to this session by a page that has been open for a human moment."""
    try:
        data = _serializer().loads(token or "")
        age = time.time() - float(data["t"])
    except Exception:  # noqa: BLE001 - any bad token is one refusal
        return False
    return data.get("n") == session.get(SESSION_NONCE_KEY) and TOKEN_MIN_SECONDS <= age <= TOKEN_MAX_SECONDS


def _same_site():
    """A browser's fetch always says where it came from; it must be this site."""
    source = request.headers.get("Origin") or request.headers.get("Referer") or ""
    return bool(source) and urlsplit(source).netloc.lower() == (request.host or "").lower()


def _needs_captcha():
    return not session.get("user_id") and not session.get(SESSION_HUMAN_KEY)


def _captcha_image():
    from .captcha import CaptchaGenerator  # noqa: PLC0415
    generator = CaptchaGenerator()
    text = generator.generate_captcha()
    session[SESSION_CAPTCHA_KEY] = text
    session.modified = True
    return generator.generate_captcha_image(text)


def _reserve_read():
    """"ok", "limit" or "blocked"; None when it cannot be checked. A read that
    cannot be counted is not made: each one is a paid call."""
    try:
        from .db import get_db  # noqa: PLC0415
        db = get_db()
        cursor = db.cursor()
        rx_photo_quota.ensure_schema(cursor)
        content_guard.ensure_schema(cursor)
        if content_guard.is_blocked(cursor, _client_ip()):
            return "blocked"
        allowed = rx_photo_quota.reserve(cursor, _client_ip(), ops_controls.get(cursor, ops_controls.RX_PER_IP),
                                         ops_controls.get(cursor, ops_controls.RX_PER_DAY))
        db.commit()
        return "ok" if allowed else "limit"
    except Exception:  # noqa: BLE001
        current_app.logger.exception("rx_photo: quota could not be checked")
        return None


def _record_flag(category, sha256):
    current_app.logger.error("ACTIVITY:RX_PHOTO_FLAGGED category=%s ip=%s", category, _client_ip())
    try:
        from .db import get_db  # noqa: PLC0415
        db = get_db()
        content_guard.flag(db.cursor(), category, sha256, _client_ip(), session.get("user_id"))
        db.commit()
        content_guard.alert(category, "photo_reader", sha256, _client_ip(), session.get("user_id"))
    except Exception:  # noqa: BLE001
        current_app.logger.exception("rx_photo: flag could not be recorded")


def register(bp):
    """Attach the reader to the main blueprint."""

    @bp.route(CHALLENGE_ROUTE, methods=["GET"])
    def rx_photo_challenge():
        out = {"ok": True, "token": _new_token(), "captcha": _needs_captcha()}
        if out["captcha"]:
            out["image"] = _captcha_image()
        return jsonify(out)

    @bp.route(VERIFY_ROUTE, methods=["POST"])
    def rx_photo_verify():
        typed = str((request.get_json(silent=True) or {}).get("captcha") or "").strip().upper()
        expected = str(session.pop(SESSION_CAPTCHA_KEY, "") or "").upper()
        session.modified = True
        if expected and typed == expected:
            session[SESSION_HUMAN_KEY] = True
            return jsonify({"ok": True})
        return jsonify({"ok": False, "code": "RX_PHOTO_CAPTCHA", "image": _captcha_image(),
                        "message": "That did not match. Please try the new code."}), 400

    @bp.route(ROUTE, methods=["POST"])
    def rx_photo_read():
        if request.form.get(HONEYPOT) or not _same_site() or not _token_ok(request.form.get("token")):
            return _refuse("RX_PHOTO_BOT", "Please reload this page and try again, or type the values below.",
                           status=403)
        if _needs_captcha():
            return _refuse("RX_PHOTO_CAPTCHA", "Please complete the check first.", status=403)
        if reads_left() <= 0:
            return _refuse("RX_PHOTO_LIMIT",
                           "You have used the photo reader a few times already. "
                           "Please type the values below, or ask us in the chat.",
                           status=429)
        upload = request.files.get(FIELD)
        if upload is None:
            return _refuse("RX_PHOTO_REJECTED",
                           "Choose a photo or PDF of your prescription first.")
        try:
            norm = lens_documents.normalise(
                upload.read(lens_documents.MAX_UPLOAD_BYTES + 1))
        except lens_documents.Rejected as exc:
            return _refuse(exc.code.replace("RX_UPLOAD", "RX_PHOTO"), exc.message)
        reserved = _reserve_read()
        if reserved is None:
            return _refuse("RX_PHOTO_PROVIDER_FAILED",
                           "We could not read your prescription right now. "
                           "Please type the values below.", status=503)
        if reserved == "blocked":
            return _refuse("RX_PHOTO_BLOCKED", "The photo reader is not available. Please type the values below.",
                           status=403)
        if reserved == "limit":
            return _refuse("RX_PHOTO_LIMIT",
                           "You have used the photo reader a few times already. "
                           "Please type the values below, or ask us in the chat.",
                           status=429)
        session[SESSION_COUNT_KEY] = int(session.get(SESSION_COUNT_KEY) or 0) + 1
        session.modified = True
        try:
            eyes, confidence, notes, model, unreadable = read_pages(norm["pages"])
        except ContentBlocked as exc:
            _record_flag(exc.category, norm["sha256"])
            return _refuse("RX_PHOTO_BLOCKED", "This image cannot be accepted. Only upload a photo of your "
                           "eyeglasses prescription. Prohibited content is blocked, logged and may be "
                           "reported to Cyber Crime India.", status=422)
        except ai_client.ModelError:
            return _refuse("RX_PHOTO_PROVIDER_FAILED",
                           "We could not read your prescription right now. "
                           "Please type the values below.", status=503)
        if unreadable or not eyes:
            return _refuse("RX_PHOTO_UNREADABLE",
                           "We could not make out the powers on that image. Try a "
                           "sharper, well-lit photo of just the prescription, or "
                           "type the values below.", status=422)
        return jsonify({"ok": True, "eyes": eyes, "confidence": confidence,
                        "notes": notes, "reads_left": reads_left()})
