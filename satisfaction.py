"""Whether a support answer solved the customer's issue, asked once.

The question is asked only where a support question has an answer the
assistant read from the customer's own record (a prescription, an order or
payment, a returned parcel) and the reply carries no navigation, offer or
escalation. Never while the customer is shopping. A session is asked at most
once. The answer is an ``ai_events`` row; no table of its own.

    asked, then "yes"  -> SATISFIED      (fixed reply, no model call)
    asked, then "no"   -> NOT_SATISFIED  (one recovery answer that ends by
                                          offering a support ticket)
    asked, then other  -> NO_RESPONSE    (derived by the report)
    never asked        -> NOT_ASKED
"""
import re

from . import ai_language

SATISFIED = "SATISFIED"
NOT_SATISFIED = "NOT_SATISFIED"
NO_RESPONSE = "NO_RESPONSE"
NOT_ASKED = "NOT_ASKED"

ASKED = "ASKED"
RECOVERY = "RECOVERY"

TERMINAL_INTENTS = (
    ai_language.INTENT_PRESCRIPTION_CONFIRMATION,
    ai_language.INTENT_PRESCRIPTION_STATUS,
    ai_language.INTENT_PRESCRIPTION_FOR_ORDER,
    ai_language.INTENT_ORDER_STATUS,
    ai_language.INTENT_PAYMENT_STATUS,
    ai_language.INTENT_RESHIP_STATUS,
)

ASK = {
    "en": "Did this answer your question? Yes / No",
    "hi-Latn": "Kya isse aapke sawaal ka jawab mila? Haan / Nahi",
    "hi": "क्या इससे आपके सवाल का जवाब मिला? हाँ / नहीं",
}

THANKS = {
    "en": "Glad that helped. I'm here if you need anything else.",
    "hi-Latn": "Khushi hui ki madad mili. Aur kuch chahiye to batayein.",
    "hi": "खुशी हुई कि मदद मिली। और कुछ चाहिए तो बताइए।",
}

TICKET_OFFER = ("Would you like me to create a support ticket so a person from "
                "our team can help?")

RECOVERY_SECTION = (
    "\nTHE CUSTOMER SAID YOUR PREVIOUS ANSWER DID NOT SOLVE THEIR ISSUE. Make one "
    "focused further attempt from the records above only: say plainly what the record "
    "shows and what it does not, and never invent a value. End with exactly this "
    "question: \"%s\"\n" % TICKET_OFFER)

_DECLINE_RE = re.compile(
    r"^\s*(no|nope|nah|not really|not at all|no it did ?n'?t|it did ?n'?t|did ?n'?t help|"
    r"not helpful|not solved|still not|nahi|nahin|nai|na|bilkul nahi|nahi mila|"
    r"\u0928\u0939\u0940\u0902|\u0928\u0939\u0940)"
    r"(\s+(really|ji|thanks|thank you))?(?!\w)[\s!.,\u0964]*$",
    re.IGNORECASE)


def is_decline(text):
    return bool(text and _DECLINE_RE.match(text.strip()))


def text_for(table, language):
    return table.get(language) or table["en"]


def pending(meta):
    """The ask the latest assistant reply made, ``{'state', 'intent'}``, or None."""
    sat = (meta or {}).get("satisfaction") or {}
    return sat if sat.get("state") == ASKED else None


def answer_of(meta, text, is_confirmation):
    """SATISFIED / NOT_SATISFIED when ``text`` answers the latest reply's ask,
    else None (a new question is not an answer)."""
    if not pending(meta):
        return None
    if is_confirmation(text):
        return SATISFIED
    if is_decline(text):
        return NOT_SATISFIED
    return None


def should_ask(turn_intent, answered, signed_in, already_asked, carries_more):
    """Ask only at a terminal support answer read from the account, once."""
    return bool(turn_intent in TERMINAL_INTENTS and answered and signed_in
                and not already_asked and not carries_more)


def already_asked(db, session_id):
    try:
        cur = db.cursor()
        cur.execute("SELECT 1 AS x FROM ai_events WHERE session_id=%s AND "
                    "event_type='SATISFACTION_ASKED' LIMIT 1", (session_id,))
        return cur.fetchone() is not None
    except Exception:  # noqa: BLE001 - an unreadable ledger never asks twice
        return True
