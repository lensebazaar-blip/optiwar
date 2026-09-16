"""
What the assistant sees in a photo the customer attached to the chat.

The photo is shown to the same vision workload the prescription upload uses
(``LENS_RX_VISION_PROVIDER``); the model answers one JSON shape, parsed here
into a ``Reading``: what kind of thing the photo shows, a short description
the assistant can say back, and — when it is a prescription — per-eye values
in the shape ``lens_rx`` already validates. Nothing here writes to the cart or
the transcript; the gateway decides what to do with the reading.
"""
import base64
import json
import os
import re

from flask import current_app

from . import ai_client
from . import lens_documents
from . import lens_rx

KIND_PRESCRIPTION = "prescription"
KIND_FRAME = "frame"
KIND_CONTACT_LENS = "contact_lens"
KIND_PRODUCT = "product"
KIND_OTHER = "other"
KINDS = (KIND_PRESCRIPTION, KIND_FRAME, KIND_CONTACT_LENS, KIND_PRODUCT, KIND_OTHER)

# Below this the model's own confidence is not enough to answer the customer
# from the photo alone; the photo goes to a person instead.
CONFIDENT = 0.6

PROMPT = (
    "A customer of an optical shop (spectacle frames, spectacle lenses, contact "
    "lenses) has attached this photo to a support chat. Look at it and answer "
    "ONLY with a JSON object, no prose, of the form:\n"
    '{"kind": "prescription|frame|contact_lens|product|other", '
    '"description": "one or two plain sentences of what is visibly in the photo", '
    '"frame": {"colour": "", "shape": "", "material": "", "rim": "full|half|rimless|", '
    '"brand_or_text": "", "condition": ""}, '
    '"prescription": {"right": {"sph": "", "cyl": "", "axis": "", "add": "", "pd": ""}, '
    '"left": {"sph": "", "cyl": "", "axis": "", "add": "", "pd": ""}, "pd": ""}, '
    '"confidence": 0.0-1.0, "unreadable": false}\n'
    "Rules: kind=prescription when the photo is a written or printed eye "
    "prescription; then transcribe only what is written (right is OD/R/RE, "
    "left is OS/L/LE; per eye SPH (also PWR/POWER/SPHERE), CYL, AXIS, ADD and PD; signed two-decimal values "
    "like \"-0.50\"; plano is \"0.00\"; leave a field \"\" when not written). "
    "kind=frame for spectacles or a spectacle frame; describe colour, shape "
    "(round, square, rectangular, cat-eye, aviator, oval, geometric), "
    "material, rim style and any visible damage. kind=contact_lens for a "
    "contact lens box, blister or lens. kind=product for other eyewear goods "
    "(case, cloth, solution). kind=other for anything else, and say what it "
    "is. Set unreadable=true when the photo is too blurred, dark or small to "
    "tell. Confidence is how sure you are of kind and description. Never "
    "include a person's name, address or any other personal detail."
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class Reading(dict):
    """The parsed answer; a dict so it stores as JSON unchanged."""

    @property
    def kind(self):
        return self.get("kind") or KIND_OTHER

    @property
    def description(self):
        return (self.get("description") or "").strip()

    @property
    def confidence(self):
        return self.get("confidence")

    @property
    def unreadable(self):
        return bool(self.get("unreadable"))

    @property
    def proposal(self):
        return self.get("proposal")

    @property
    def confident(self):
        """Readable, recognised, and sure enough to be answered from."""
        if self.unreadable or self.kind == KIND_OTHER:
            return False
        if self.kind == KIND_PRESCRIPTION and not self.proposal:
            return False
        return self.confidence is not None and self.confidence >= CONFIDENT


def workload():
    return lens_documents.provider_name(
        current_app.config.get("LENS_RX_VISION_PROVIDER")
        or os.environ.get("LENS_RX_VISION_PROVIDER"))


def messages_for(data, mime_type):
    return [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {
            "url": "data:%s;base64,%s" % (mime_type, base64.b64encode(data).decode("ascii")),
            "detail": "high"}},
    ]}]


def parse(text):
    """A ``Reading`` from the model's answer; garbage is an unreadable one."""
    cleaned = _FENCE.sub("", (text or "").strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    raw = None
    if start >= 0 and end > start:
        try:
            raw = json.loads(cleaned[start:end + 1])
        except ValueError:
            raw = None
    if not isinstance(raw, dict):
        return Reading(kind=KIND_OTHER, description="", confidence=None,
                       unreadable=True, proposal=None)
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in KINDS:
        kind = KIND_OTHER
    confidence = raw.get("confidence")
    try:
        confidence = round(min(1.0, max(0.0, float(confidence))), 2)
    except (TypeError, ValueError):
        confidence = None
    proposal = None
    rx = raw.get("prescription")
    if kind == KIND_PRESCRIPTION and isinstance(rx, dict):
        try:
            proposal = lens_rx.proposal_from_mapping(
                {eye: rx.get(eye) for eye in ("right", "left")})
        except Exception:  # noqa: BLE001 - a value the canoniser refuses is no reading
            proposal = None
    frame = raw.get("frame") if isinstance(raw.get("frame"), dict) else {}
    frame = {k: str(v).strip()[:80] for k, v in frame.items()
             if k in ("colour", "shape", "material", "rim", "brand_or_text", "condition")
             and v not in (None, "")}
    pd = ""
    if isinstance(rx, dict):
        pd = str(rx.get("pd") or "").strip()[:20]
    return Reading(kind=kind,
                   description=str(raw.get("description") or "").strip()[:600],
                   frame=frame, pd=pd, proposal=proposal, confidence=confidence,
                   unreadable=raw.get("unreadable") is True)


def describe(data, mime_type, endpoint="/api/chat/attachment"):
    """Ask the configured vision workload about the photo.

    Returns ``(reading, model_name)``. Provider failures propagate as
    ``ai_client.ModelError`` so the caller can treat "could not look" apart
    from "looked and could not tell".
    """
    wl = workload()
    resp = ai_client.call_model(
        workload=wl, messages=messages_for(data, mime_type),
        max_tokens=500, temperature=0, endpoint=endpoint)
    text = ""
    try:
        text = resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        pass
    return parse(text), getattr(resp, "model", None) or wl


def _eye_line(label, values):
    parts = []
    for key, name in (("sph", "SPH"), ("cyl", "CYL"), ("axis", "AXIS"), ("add", "ADD")):
        v = values.get(key) if values else None
        if v not in (None, ""):
            parts.append("%s %s" % (name, v))
    return "%s: %s" % (label, ", ".join(parts) if parts else "nothing written")


def customer_reply(reading, filename=""):
    """What the assistant says about the photo, in its own words."""
    if reading.kind == KIND_PRESCRIPTION and reading.proposal:
        lines = ["Thanks — I read your prescription photo as:"]
        lines.append(_eye_line("Right eye (OD)", reading.proposal.get("right")))
        lines.append(_eye_line("Left eye (OS)", reading.proposal.get("left")))
        if reading.get("pd"):
            lines.append("PD: %s" % reading["pd"])
        lines.append("Please check these against your paper — tell me if anything "
                     "is wrong. Nothing is applied until you confirm.")
        return "\n".join(lines)
    if reading.kind == KIND_FRAME and reading.confident:
        f = reading.get("frame") or {}
        bits = [f.get(k) for k in ("colour", "shape", "material") if f.get(k)]
        head = "This looks like a %s frame" % " ".join(bits) if bits else "This looks like a spectacle frame"
        if f.get("rim") and f["rim"] not in ("full",):
            head += " (%s rim)" % f["rim"]
        tail = []
        if f.get("brand_or_text"):
            tail.append("I can see \"%s\" on it." % f["brand_or_text"])
        if f.get("condition"):
            tail.append("Condition: %s." % f["condition"])
        text = head + "."
        if reading.description:
            text += " " + reading.description
        if tail:
            text += " " + " ".join(tail)
        return text + " What would you like to do — find similar frames, a repair, or something else?"
    if reading.confident and reading.description:
        return reading.description + " How can I help with it?"
    return ""


def context_line(filename, reading):
    """One line of system-prompt context so the text model knows the photo."""
    if reading is None:
        return "- %s: not analysed." % filename
    if reading.unreadable:
        return "- %s: the vision model could not read it." % filename
    body = reading.description or "(no description)"
    if reading.kind == KIND_PRESCRIPTION and reading.proposal:
        body += " Values read: %s; %s." % (
            _eye_line("right", reading.proposal.get("right")),
            _eye_line("left", reading.proposal.get("left")))
    return "- %s (%s, confidence %s): %s" % (
        filename, reading.kind,
        "n/a" if reading.confidence is None else reading.confidence, body)


def prompt_section(rows):
    """``rows`` are ``(filename, Reading-or-None)``; '' when there are none."""
    if not rows:
        return ""
    lines = ["PHOTOS THE CUSTOMER ATTACHED IN THIS CHAT (already analysed by the "
             "vision model — you HAVE seen these photos; never say you cannot view "
             "images. Answer from these descriptions; if they are not enough, say "
             "what is unclear and offer to pass the photo to a support person):"]
    lines.extend(context_line(name, reading) for name, reading in rows)
    return "\n".join(lines)
