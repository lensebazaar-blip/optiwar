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

**Find, Read, Compare** (owner, 9 Nov 2026; Ops switch ``rx_find_read_compare``
on /ops/controls, off returns to the single read below):

* **Find** sorts up to 6 small pages, reads no powers, and carries the content
  check (a Haiku flag is confirmed by Opus, as for every upload).
* **Read** sends one page, upright and sharp, to Claude Haiku and Claude Opus
  at once. Pages that are not a spectacle table never reach a reader, so
  a contact-lens note cannot be copied. Two prescriptions: the customer chooses.
* **Compare** fills what both read alike. Anything else stays amber for the
  customer to settle against the paper, and the page will not continue until
  they have. "Doubtful" and "not written" are different: only "not written"
  may become CYL 0.00.
"""
import datetime
import hashlib
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
FIELDS = ("sph", "cyl", "axis", "add")
FIELD_LABELS = {"sph": "SPH", "cyl": "CYL", "axis": "AXIS", "add": "ADD"}

READER_PAGES = content_guard.MAX_PAGES
FIND_SIDE = 1100                    # the longest side of a page Find looks at
FIND_EFFORT = "medium"
READ_EFFORT = "medium"
FIND_TOKENS = 2048
READ_TOKENS = 4096                  # thinking counts against this
BACKUP_TOKENS = 800
READ_SECONDS = 20
READ_RESERVE = 6                    # kept back so a backup can still answer if Claude cannot
MEMO_SECONDS = 2 * 60 * 60
MEMO_SIZE = 6
CHOICE_SECONDS = 10 * 60
CHOICE_SALT = "rx-photo-choice"
PROMPT_VERSION = "1"                # a new prompt must not reuse remembered answers
SESSION_MEMO_KEY = "rx_photo_memo"
SESSION_CHOICE_KEY = "rx_photo_choice_reads"
SPECTACLE_TABLE = "spectacle_table"
PAGE_KINDS = (SPECTACLE_TABLE, "contact_lens", "auto_refraction", "visit_notes", "medicine", "report", "other")

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
    "The page may be sideways or upside down: read it in whichever direction "
    "the text runs. Use the spectacle powers only: ignore any "
    "powers marked CL or C/L. If a PDF has several pages, use "
    "the page with the spectacle power table.\n"
    "Rules: 'right' is OD / R / RE / Right Eye; 'left' is OS / L / LE / Left "
    "Eye. sph is SPH, SPHERE, PWR, POWER or DS. cyl is CYL or CYLINDER. axis is "
    "AXIS (1 to 180). add is ADD, ADDITION, NEAR ADD or N.V. (a positive "
    "reading power). With separate DISTANCE and NEAR rows, give the distance "
    "row as sph/cyl/axis. A NEAR row with only a SPH value and a dash for CYL "
    "and AXIS, or one marked Add, IS the addition: give that value as add. "
    "Only when the NEAR row repeats a full power, give the difference between "
    "near and distance SPH as add. Use signed "
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

FIND_PROMPT = (
    "These images are the pages of a file a customer uploaded to an optical shop, each after its label. "
    "Sort every page. Do not read or report any powers.\n"
    "Return ONLY a JSON object: "
    '{"pages": [{"page": 1, "kind": "spectacle_table", "date": "2025-07-02", "rotate": 0}], "content_flag": "none"}\n'
    "kind is one of:\n"
    "- spectacle_table: a prescription for glasses set out as a table or grid with SPH, CYL and AXIS (or "
    "DISTANCE and NEAR rows) for each eye, handwritten or typed. A page that holds such a table is this "
    "kind even if it also has other notes.\n"
    "- contact_lens: a contact lens prescription, with BC or DIA columns or headed contact lens.\n"
    "- auto_refraction: machine readings (AR, autorefractor, K readings) with no prescription table.\n"
    "- visit_notes: a doctor's notes where powers appear only as loose handwriting or margin notes.\n"
    "- medicine: a list of medicines or eye drops.\n"
    "- report: a test or investigation report.\n"
    "- other: anything else.\n"
    "date is the date written on that prescription as YYYY-MM-DD, or \"\" if there is none. Dates on Indian "
    "prescriptions are day first: 20/9/23 is 2023-09-20.\n"
    "rotate is how many degrees clockwise the page must be turned so that its writing reads upright: "
    "0, 90, 180 or 270.\n"
    "content_flag is judged from every page: \"child_sexual\" for any sexualised depiction of a minor; "
    "\"sexual\" for nudity or sexual content; \"extremist\" for terrorist or violent extremist material; "
    "\"illegal\" for other clearly unlawful material; \"graphic\" for graphic violence or gore; otherwise "
    "\"none\". An ordinary photo or document is \"none\" even when it is not a prescription."
)
FIND_SCHEMA = {
    "type": "object",
    "properties": {
        "pages": {"type": "array", "items": {
            "type": "object",
            "properties": {"page": {"type": "integer"}, "kind": {"type": "string", "enum": list(PAGE_KINDS)},
                           "date": {"type": "string"}, "rotate": {"type": "integer", "enum": [0, 90, 180, 270]}},
            "required": ["page", "kind", "date", "rotate"], "additionalProperties": False}},
        "content_flag": {"type": "string", "enum": ["none"] + list(BLOCKED_CATEGORIES)}},
    "required": ["pages", "content_flag"], "additionalProperties": False}

READ_PROMPT = (
    "This image is one page holding an EYEGLASSES (spectacle) prescription. Transcribe only the prescription "
    "table for glasses. Never use margin notes, loose handwriting, powers marked CL or C/L, machine "
    "readings or anything outside that table.\n"
    "Return ONLY a JSON object. Every field of each eye has this shape: "
    '{"value": "-3.75", "written": true, "doubtful": false}. The object is: '
    '{"right": {"sph": field, "cyl": field, "axis": field, "add": field}, '
    '"left": {"sph": field, "cyl": field, "axis": field, "add": field}, '
    '"source": {"kind": "spectacle_table", "row": "DISTANCE"}}\n'
    "written is false, with value \"\", when the table has nothing for that field. Set doubtful to true "
    "whenever a digit, a sign or a decimal could be read two ways, even if you have a preference; give your "
    "best reading as value. Never guess a field to complete the table.\n"
    "right is OD / R / RE / Right Eye; left is OS / L / LE / Left Eye. sph is SPH, SPHERE, PWR, POWER or DS. "
    "cyl is CYL or CYLINDER. axis is AXIS (1 to 180). add is ADD, ADDITION, NEAR ADD or N.V. With separate "
    "DISTANCE and NEAR rows, use the distance row for sph, cyl and axis. A NEAR row with only a SPH value "
    "and a dash for CYL and AXIS, or one marked Add, IS the addition: give that value as add. Only when the "
    "NEAR row repeats a full power, give the difference between near and distance SPH as add. Use signed "
    "decimals with two places (\"-0.50\", \"+1.25\"); plano, PL or a dash is \"0.00\" with written true. "
    "source.kind is spectacle_table when you read such a table, otherwise other; source.row names the row "
    "you used. Never include the patient's name, age, phone or any other personal detail."
)
_READ_FIELD = {"type": "object",
               "properties": {"value": {"type": "string"}, "written": {"type": "boolean"}, "doubtful": {"type": "boolean"}},
               "required": ["value", "written", "doubtful"], "additionalProperties": False}
_READ_EYE = {"type": "object", "properties": {name: _READ_FIELD for name in FIELDS},
             "required": list(FIELDS), "additionalProperties": False}
READ_SCHEMA = {
    "type": "object",
    "properties": {"right": _READ_EYE, "left": _READ_EYE,
                   "source": {"type": "object",
                              "properties": {"kind": {"type": "string", "enum": [SPECTACLE_TABLE, "other"]},
                                             "row": {"type": "string"}},
                              "required": ["kind", "row"], "additionalProperties": False}},
    "required": ["right", "left", "source"], "additionalProperties": False}

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
    def __init__(self, category, checked_by=None):
        super().__init__(category)
        self.category, self.checked_by = category, checked_by


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


def _usable(text):
    """An answer worth stopping at: a content flag, or powers we can use."""
    if content_flag(text):
        return True
    eyes, _confidence, _notes, unreadable = parse_answer(text)
    return bool(eyes) and not unreadable


def _count(provider):
    """Today's tally on /ops/controls. Best effort."""
    try:
        from .db import get_db  # noqa: PLC0415
        db = get_db()
        content_guard.count_check(db, db.cursor(), provider)
    except Exception:  # noqa: BLE001
        pass


def read_pages(pages, endpoint=ROUTE):
    """Ask each AI in the owner's order; ``(eyes, confidence, notes, model, unreadable)``.

    ``ai_client.ModelError`` when no provider answered at all.
    """
    text, name = content_guard.ask_in_order(pages, "image/jpeg", PROMPT, _usable, max_tokens=400, detail="high",
                                            endpoint=endpoint, claude_max_tokens=2048, flagged=content_flag)
    current_app.logger.info("ACTIVITY:RX_PHOTO_READ provider=%s answered=%s", name, bool(text))
    _count(name or content_guard.NOT_CHECKED)
    if not text:
        raise ai_client.ModelError("no provider answered")
    category = content_flag(text)
    if category:
        raise ContentBlocked(category, name)
    eyes, confidence, notes, unreadable = parse_answer(text)
    return eyes, confidence, notes, name, unreadable


# --------------------------------------------------------------------------
# Find, Read, Compare
# --------------------------------------------------------------------------

def _json_object(text):
    cleaned = _FENCE.sub("", (text or "").strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        raw = json.loads(cleaned[start:end + 1])
    except ValueError:
        return None
    return raw if isinstance(raw, dict) else None


def _image_ops():
    from PIL import Image  # noqa: PLC0415 - optional at import
    import io  # noqa: PLC0415
    return Image, io


def _fit(jpeg, side):
    """The page no longer than ``side``: Find sorts pages, it does not read digits."""
    Image, io = _image_ops()
    image = Image.open(io.BytesIO(jpeg))
    if max(image.size) <= side:
        return jpeg
    image.thumbnail((side, side), Image.LANCZOS)
    out = io.BytesIO()
    image.convert("RGB").save(out, format="JPEG", quality=80)
    return out.getvalue()


def _turned(jpeg, rotate):
    """The page turned ``rotate`` degrees clockwise. The file itself cannot say
    a scan is sideways, so this comes from Find."""
    if rotate not in (90, 180, 270):
        return jpeg
    Image, io = _image_ops()
    out = io.BytesIO()
    Image.open(io.BytesIO(jpeg)).rotate(-rotate, expand=True).convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue()


def _found_pages(text, count):
    """The usable rows of a Find answer, for pages that exist; [] when there are none."""
    raw = _json_object(text)
    pages = raw.get("pages") if raw else None
    if not isinstance(pages, list):
        return []
    out, seen = [], set()
    for row in pages:
        if not isinstance(row, dict) or isinstance(row.get("page"), bool) or not isinstance(row.get("page"), int):
            continue
        if not 1 <= row["page"] <= count or row["page"] in seen or row.get("kind") not in PAGE_KINDS:
            continue
        seen.add(row["page"])
        out.append({"page": row["page"], "kind": row["kind"], "date": str(row.get("date") or ""),
                    "rotate": row.get("rotate") if row.get("rotate") in (90, 180, 270) else 0})
    return out


def find_pages(pages):
    """``(rows, which AI)`` for up to READER_PAGES pages.

    ``ContentBlocked`` for a flag that stood; ``ai_client.ModelError`` when nothing
    usable came back, which is also the outcome when Opus could not confirm a flag.
    """
    small = [_fit(page, FIND_SIDE) for page in pages]
    text, name = content_guard.ask_in_order(
        small, "image/jpeg", FIND_PROMPT, lambda t: bool(content_flag(t) or _found_pages(t, len(small))),
        max_tokens=600, detail="high", endpoint=ROUTE, claude_max_tokens=FIND_TOKENS, flagged=content_flag,
        effort=FIND_EFFORT, schema=FIND_SCHEMA, label_pages=True)
    current_app.logger.info("ACTIVITY:RX_PHOTO_READ step=find provider=%s answered=%s", name, bool(text))
    _count(name or content_guard.NOT_CHECKED)
    category = content_flag(text)
    if category:
        raise ContentBlocked(category, name)
    rows = _found_pages(text, len(small))
    if not rows:
        raise ai_client.ModelError("no usable sorting of the pages")
    return rows, name


def _when(text):
    """The prescription's date, or None for anything that is not a believable one."""
    try:
        day = datetime.datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return day if datetime.date(1990, 1, 1) <= day <= datetime.date.today() else None


def _choice_label(row, first):
    day = _when(row["date"])
    if day is None:
        return "Page %d (no date found)" % row["page"]
    return "%d %s %d, page %d%s" % (day.day, day.strftime("%B"), day.year, row["page"], " (newest)" if first else "")


def tables_newest_first(rows):
    """The spectacle-table pages, newest first and undated last."""
    tables = [r for r in rows if r["kind"] == SPECTACLE_TABLE]
    return sorted(tables, key=lambda r: (_when(r["date"]) is None,
                                         -(_when(r["date"]) or datetime.date.min).toordinal(), r["page"]))


def _select_value(field, value):
    """``(option value or "", number or None, changed)`` for what a reader wrote."""
    number = _number(value)
    if field == "axis":
        snapped, changed = snap_axis(value)
    else:
        snapped, changed = snap(value, {"sph": SPH_BANDS, "cyl": CYL_BANDS, "add": ADD_BANDS}[field])
    if snapped is not None and field == "cyl" and abs(snapped) < 1e-9:
        snapped = 0.0
    return option_value(field, snapped), number, changed


def _reader_field(field, cell):
    """One field as ``{"written", "doubtful", "option", "changed", "outside"}``; the
    old flat shape (a bare string, from a backup reader) is read as written if not empty."""
    if isinstance(cell, dict):
        value, written, doubtful = cell.get("value"), cell.get("written") is True, cell.get("doubtful") is True
    else:
        value, written, doubtful = cell, str(cell or "").strip() != "", False
    option, number, changed = _select_value(field, value) if written else ("", None, False)
    if field == "add" and number is not None and number <= 0:   # 0.00 ADD is no addition
        return {"written": False, "doubtful": False, "option": "", "changed": False, "outside": False}
    if written and number is None:      # says it read something, gives nothing usable
        doubtful = True
    return {"written": written, "doubtful": doubtful, "option": option, "changed": bool(option and changed),
            "outside": bool(number is not None and not option)}


def _reader_fields(raw):
    out = {}
    for eye in EYES:
        values = raw.get(eye) if isinstance(raw.get(eye), dict) else {}
        out[eye] = {f: _reader_field(f, values.get(f, values.get("pwr") if f == "sph" else None)) for f in FIELDS}
    return out


def _decide(cells):
    """``(value, check, options, absent)`` for one field across the readers."""
    written = [c for c in cells if c["written"]]
    doubtful = any(c["doubtful"] for c in cells)
    if not written and not doubtful:
        return "", False, [], True
    options = list(dict.fromkeys(c["option"] for c in written if c["option"]))
    agreed = len(cells) == 2 and len(written) == 2 and not doubtful and len(options) == 1 and all(c["option"] for c in written)
    if agreed:
        return options[0], False, options, False
    if len(cells) == 1 and options:     # one reader: shown, but the customer must confirm it
        return options[0], True, options, False
    return "", True, options, False


def compare(answers):
    """``(eyes, check, notes)`` from one or two readers' JSON. ``eyes`` has what may
    be shown in the selects; ``check`` lists every field the customer must settle."""
    readers = [_reader_fields(raw) for raw in answers]
    single = len(readers) == 1
    eyes, check, notes = {}, [], []
    for eye in EYES:
        name = "Right" if eye == "right" else "Left"
        decided = {f: _decide([r[eye][f] for r in readers]) for f in FIELDS}
        if all(decided[f][3] for f in FIELDS):
            eyes[eye] = None
            continue
        values = {f: decided[f][0] for f in FIELDS}
        amber = {f: decided[f][1] for f in FIELDS}
        options = {f: decided[f][2] for f in FIELDS}
        if decided["sph"][3]:           # an eye always has a power: not to be assumed
            amber["sph"] = True
        if decided["cyl"][3]:
            values["cyl"] = "0.00"      # both say none is written
        if decided["axis"][3]:
            if values["cyl"] == "0.00" and not amber["cyl"]:
                values["axis"] = "0"
            else:
                amber["axis"] = True
        minus_sph = values["sph"] and float(values["sph"]) < 0
        plus_cyl = values["cyl"] and float(values["cyl"]) > 0
        if minus_sph and plus_cyl:      # how a contact-lens note is often written; legal on glasses, so ask
            for f in ("cyl", "axis"):
                options[f] = options[f] or ([values[f]] if values[f] else [])
                values[f], amber[f] = "", True
        if single:
            for f in FIELDS:
                if values[f]:
                    amber[f], options[f] = True, options[f] or [values[f]]
        eyes[eye] = values
        for f in FIELDS:
            if any(r[eye][f]["outside"] for r in readers):
                notes.append("%s %s is outside the range sold here; please choose it yourself." % (name, FIELD_LABELS[f]))
            elif not amber[f] and values[f] and any(r[eye][f]["changed"] for r in readers):
                notes.append("%s %s rounded to the nearest step." % (name, FIELD_LABELS[f]))
            if amber[f]:
                check.append({"eye": eye, "field": f, "label": "%s %s" % (name, FIELD_LABELS[f]), "options": options[f]})
    return eyes, check, notes


def _read_usable(text):
    raw = _json_object(text)
    return bool(raw and any(isinstance(raw.get(eye), dict) for eye in EYES))


def _from_table(raw):
    source = raw.get("source")
    return isinstance(source, dict) and source.get("kind") == SPECTACLE_TABLE


def read_page(page, seconds=None):
    """``(eyes, check, notes, readers)`` for one upright page; eyes is None when
    no reader found a spectacle table on it. ``ai_client.ModelError`` when none answered."""
    left = content_guard.BUDGET if seconds is None else seconds
    started = time.monotonic()
    together = max(content_guard.MIN_ATTEMPT, min(READ_SECONDS, left - READ_RESERVE))
    answers = content_guard.ask_claude_together([page], "image/jpeg", READ_PROMPT, READ_TOKENS, READ_EFFORT, READ_SCHEMA, together)
    heard, good, names = 0, [], []
    for name, text in answers:
        usable = _read_usable(text)
        _count(name if usable else content_guard.NOT_CHECKED)
        if not usable:
            continue
        heard += 1
        raw = _json_object(text)
        if _from_table(raw):
            good.append(raw)
            names.append(name)
    if not heard:
        text, name = content_guard.ask_in_order(
            [page], "image/jpeg", READ_PROMPT, _read_usable, max_tokens=BACKUP_TOKENS, detail="high", endpoint=ROUTE,
            claude_max_tokens=READ_TOKENS, claude=False, budget=left - (time.monotonic() - started))
        _count(name or content_guard.NOT_CHECKED)
        raw = _json_object(text) if name else None
        if raw is None:
            raise ai_client.ModelError("no reader answered")
        if _from_table(raw) or not isinstance(raw.get("source"), dict):     # the old flat shape has no source
            good, names = [raw], [name]
    current_app.logger.info("ACTIVITY:RX_PHOTO_READ step=read readers=%s", ",".join(names) or "none")
    if not good:
        return None, [], [], names
    eyes, check, notes = compare(good)
    return (eyes if any(eyes.values()) else None), check, notes, names


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


def _record_flag(category, sha256, checked_by):
    current_app.logger.error("ACTIVITY:RX_PHOTO_FLAGGED category=%s ip=%s", category, _client_ip())
    try:
        from .db import get_db  # noqa: PLC0415
        db = get_db()
        content_guard.flag(db.cursor(), category, sha256, _client_ip(), session.get("user_id"), checked_by=checked_by)
        db.commit()
        content_guard.alert(category, "photo_reader", sha256, _client_ip(), session.get("user_id"), checked_by)
    except Exception:  # noqa: BLE001
        current_app.logger.exception("rx_photo: flag could not be recorded")


def _two_step_on():
    try:
        from .db import get_db  # noqa: PLC0415
        return bool(ops_controls.get(get_db().cursor(), ops_controls.RX_TWO_STEP))
    except Exception:  # noqa: BLE001
        return bool(ops_controls.SETTINGS[ops_controls.RX_TWO_STEP]["default"])


def _memo_key(sha256, step):
    return "%s:%s:%s" % (PROMPT_VERSION, sha256, step)


def _remembered(sha256, step):
    entry = (session.get(SESSION_MEMO_KEY) or {}).get(_memo_key(sha256, step))
    return entry["r"] if entry and time.time() - entry["t"] <= MEMO_SECONDS else None


def _remember(sha256, step, result):
    """The same file gives the same answer for this customer, and no second read.
    The values only, in the session; the image is still never kept."""
    memo = dict(session.get(SESSION_MEMO_KEY) or {})
    memo[_memo_key(sha256, step)] = {"t": time.time(), "r": result}
    while len(memo) > MEMO_SIZE:
        memo.pop(min(memo, key=lambda k: memo[k]["t"]))
    session[SESSION_MEMO_KEY] = memo
    session.modified = True


def _choice_serializer():
    from itsdangerous import URLSafeSerializer  # noqa: PLC0415
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt=CHOICE_SALT)


def _choice_token(sha256, page, rotate):
    return _choice_serializer().dumps({"s": sha256, "p": page, "r": rotate,
                                       "n": session.get(SESSION_NONCE_KEY), "t": time.time()})


def _open_choice(token, sha256):
    """``(page, rotate)`` from a token this session was given for this file, else None."""
    try:
        data = _choice_serializer().loads(token)
        age = time.time() - float(data["t"])
    except Exception:  # noqa: BLE001 - any bad token is one refusal
        return None
    if data.get("s") != sha256 or data.get("n") != session.get(SESSION_NONCE_KEY) or age > CHOICE_SECONDS:
        return None
    return int(data["p"]), int(data["r"])


def _spend_choice(sha256):
    """Choosing costs no read, but a file has only so many pages to choose from."""
    spent = session.get(SESSION_CHOICE_KEY) or {}
    used = int(spent.get("n") or 0) if spent.get("s") == sha256 else 0
    if used >= READER_PAGES:
        return False
    session[SESSION_CHOICE_KEY] = {"s": sha256, "n": used + 1}
    session.modified = True
    return True


def _reply(result, sha256):
    out = dict(result, ok=True, confidence=None, reads_left=reads_left())
    if "choices" in out:
        out["choices"] = [dict(c, token=_choice_token(sha256, c["page"], c["rotate"])) for c in out["choices"]]
    return jsonify(out)


LIMIT_MESSAGE = "You have used the photo reader a few times already. Please type the values below, or ask us in the chat."
TRY_AGAIN_MESSAGE = "We could not read your prescription right now. Please try again in a minute or type the values below."
NO_TABLE_MESSAGE = ("We could not find a spectacle prescription table in that file. Try a sharper photo of just the "
                    "prescription, or type the values below.")
BLOCKED_MESSAGE = ("This image cannot be accepted. Only upload a photo of your eyeglasses prescription. Prohibited "
                   "content is blocked, logged and may be reported to Cyber Crime India.")


def _read_in_two_steps():
    """The route's body when the Ops switch is on. See the top of this file."""
    upload = request.files.get(FIELD)
    if upload is None:
        return _refuse("RX_PHOTO_REJECTED", "Choose a photo or PDF of your prescription first.")
    data = upload.read(lens_documents.MAX_UPLOAD_BYTES + 1)
    sha256 = hashlib.sha256(data).hexdigest()
    chosen = None
    if request.form.get("choice"):
        chosen = _open_choice(request.form["choice"], sha256)
        if chosen is None:
            return _refuse("RX_PHOTO_CHOICE_EXPIRED", "That choice has expired. Please scan your prescription again.")
    step = "page%d" % chosen[0] if chosen else "file"
    remembered = _remembered(sha256, step)
    if remembered is not None:
        return _reply(remembered, sha256)
    if chosen is None and reads_left() <= 0:
        return _refuse("RX_PHOTO_LIMIT", LIMIT_MESSAGE, status=429)
    if chosen is not None and not _spend_choice(sha256):
        return _refuse("RX_PHOTO_LIMIT", LIMIT_MESSAGE, status=429)
    try:
        norm = lens_documents.normalise(data, pdf_pages=READER_PAGES)
    except lens_documents.Rejected as exc:
        return _refuse(exc.code.replace("RX_UPLOAD", "RX_PHOTO"), exc.message)
    started, notes_before = time.monotonic(), []
    if chosen is None:
        reserved = _reserve_read()
        if reserved is None:
            return _refuse("RX_PHOTO_PROVIDER_FAILED", TRY_AGAIN_MESSAGE, status=503)
        if reserved == "blocked":
            return _refuse("RX_PHOTO_BLOCKED", "The photo reader is not available. Please type the values below.", status=403)
        if reserved == "limit":
            return _refuse("RX_PHOTO_LIMIT", LIMIT_MESSAGE, status=429)
        session[SESSION_COUNT_KEY] = int(session.get(SESSION_COUNT_KEY) or 0) + 1
        session.modified = True
    try:
        if chosen is None:
            rows, _by = find_pages(norm["pages"])
            tables = tables_newest_first(rows)
            if norm["kind"] == lens_documents.KIND_PDF and lens_documents.pdf_page_count(data) > READER_PAGES:
                notes_before.append("We looked at the first %d pages. If your prescription is later in the file, "
                                    "please upload just that page." % READER_PAGES)
            if not tables:
                return _refuse("RX_PHOTO_UNREADABLE", NO_TABLE_MESSAGE, status=422)
            if len(tables) > 1:
                result = {"choose": True, "notes": notes_before,
                          "choices": [{"page": r["page"], "rotate": r["rotate"], "label": _choice_label(r, i == 0),
                                       "selected": i == 0} for i, r in enumerate(tables)]}
                _remember(sha256, step, result)
                return _reply(result, sha256)
            chosen = (tables[0]["page"], tables[0]["rotate"])
        page, rotate = chosen
        if page > len(norm["pages"]):
            return _refuse("RX_PHOTO_UNREADABLE", NO_TABLE_MESSAGE, status=422)
        jpeg = lens_documents.pdf_page_jpeg(data, page - 1) if norm["kind"] == lens_documents.KIND_PDF else norm["pages"][page - 1]
        eyes, check, notes, _readers = read_page(_turned(jpeg, rotate), content_guard.BUDGET - (time.monotonic() - started))
    except ContentBlocked as exc:
        _record_flag(exc.category, norm["sha256"], exc.checked_by)
        return _refuse("RX_PHOTO_BLOCKED", BLOCKED_MESSAGE, status=422)
    except Exception as exc:  # noqa: BLE001 - this page must always get an answer it can show
        if not isinstance(exc, ai_client.ModelError):
            current_app.logger.exception("rx_photo: reading failed")
        return _refuse("RX_PHOTO_PROVIDER_FAILED", TRY_AGAIN_MESSAGE, status=503)
    if not eyes:
        return _refuse("RX_PHOTO_UNREADABLE", NO_TABLE_MESSAGE, status=422)
    result = {"eyes": eyes, "check": check, "notes": notes_before + notes, "page": page}
    _remember(sha256, step, result)
    return _reply(result, sha256)


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
        if _two_step_on():
            return _read_in_two_steps()
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
            _record_flag(exc.category, norm["sha256"], exc.checked_by)
            return _refuse("RX_PHOTO_BLOCKED", "This image cannot be accepted. Only upload a photo of your "
                           "eyeglasses prescription. Prohibited content is blocked, logged and may be "
                           "reported to Cyber Crime India.", status=422)
        except Exception as exc:  # noqa: BLE001 - this page must always get an answer it can show
            if not isinstance(exc, ai_client.ModelError):
                current_app.logger.exception("rx_photo: reading failed")
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
